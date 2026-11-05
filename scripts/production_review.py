"""Offline production-stage checks, not human identity or originality certification.

Only retained user messages can be recorded as approvals. Agents must not invent
them. Local evidence can be forged by an actor who controls all files; this
module checks version/sequence integrity, not the authenticity of the speaker.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOLDS_PATH = ROOT / 'references/editorial_holds.json'
ALLOWED_WORKFLOWS = {
    'JACKIE-WORKFLOW-2026-10-20-R2',
    'JACKIE-WORKFLOW-2026-10-27-R7',
    'JACKIE-WORKFLOW-2026-10-28-R8',
    'JACKIE-WORKFLOW-2026-10-29-R10',
}
WORKFLOW = 'JACKIE-WORKFLOW-2026-10-29-R10'
ARTIFACTS = {'topic': 'review/topic_brief.json', 'sample': 'review/sample.md',
             'manuscript': 'content.json'}
ASSESSMENTS = ('originality', 'freshness', 'reader_value', 'company_claims')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def confined_file(folder, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('阶段证据必须是稿件内相对文件')
    path = (folder / relative).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        raise ValueError('阶段证据缺失或越界：' + relative)
    return path


def editorial_hold(folder):
    """Fail closed if the centrally maintained hold register is unavailable."""
    try:
        value = json.loads(HOLDS_PATH.read_text(encoding='utf-8'))
        if value.get('schema') != 1 or not isinstance(value.get('items'), dict):
            raise ValueError('冻结表格式无效')
        folder = Path(folder).resolve()
        item = value['items'].get(folder.name)
        source = folder/'content.json'
        if item is None and source.is_file():
            copied_hash = digest(source)
            item = next((entry for entry in value['items'].values()
                         if isinstance(entry, dict) and entry.get('content_sha256') == copied_hash), None)
        if item is None:
            return None
        if not isinstance(item, dict) or item.get('status') != 'paused':
            raise ValueError('冻结项无效；恢复须真实用户裁决后移出并保留历史')
        return item
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {'status': 'paused', 'reason': '无法确认编辑暂停状态：' + str(exc)}


def _snapshot(folder, entry):
    from scripts.review_contract import snapshot_errors
    errors = snapshot_errors(folder, entry, '生产阶段')
    if errors:
        raise ValueError('; '.join(errors))
    return confined_file(folder, entry['evidence_file'])


def _approval(folder, entry, stage, previous, now):
    if (not isinstance(entry, dict) or entry.get('status') != 'approved'
            or entry.get('actor_type') != 'user' or entry.get('source') != 'user_message'
            or not str(entry.get('actor', '')).strip() or entry.get('scope') != stage):
        raise ValueError(stage + ' 缺少真实用户阶段批准；AI自审不能替代')
    artifact = confined_file(folder, ARTIFACTS[stage])
    if entry.get('artifact_sha256') != digest(artifact):
        raise ValueError(stage + ' 批准未绑定当前文件，修改后须重新确认')
    snapshot_path = _snapshot(folder, entry)
    snapshot = snapshot_path.read_text(encoding='utf-8')
    quote = entry.get('user_quote')
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError(stage + ' 批准原话为空')
    # Historical plain text snapshots retain the byte hash + quote checks.
    # JSON snapshots additionally bind their structured context to the entry.
    is_structured = True
    try:
        structured = json.loads(snapshot)
    except ValueError:
        if snapshot_path.suffix.lower() == '.json' or snapshot.lstrip().startswith(('{', '[')):
            raise ValueError(stage + ' 结构化批准快照格式错误')
        is_structured = False
        structured = None
    if is_structured:
        if (not isinstance(structured, dict)
                or structured.get('stage') != stage
                or structured.get('artifact_sha256') != entry.get('artifact_sha256')
                or structured.get('user_quote') != quote
                or ('artifact' in structured and structured['artifact'] != ARTIFACTS[stage])):
            raise ValueError(stage + ' 结构化批准快照阶段、产物摘要或原话不一致')
        for key in ('actor', 'actor_type', 'source', 'approved_at'):
            if key in structured and structured[key] != entry.get(key):
                raise ValueError(stage + ' 结构化批准快照字段不一致：' + key)
    elif quote not in snapshot:
        raise ValueError(stage + ' 批准原话未定位到留存用户消息')
    when = datetime.fromisoformat(str(entry.get('approved_at', '')).replace('Z', '+00:00'))
    if when.tzinfo is None or when > now or (previous and when < previous):
        raise ValueError(stage + ' 批准时间缺时区、在未来或顺序倒置')
    return when


def review_errors(folder, stage='delivery', now=None):
    """topic -> sample -> delivery; no approval is created or migrated here."""
    if stage not in ('topic', 'sample', 'delivery'):
        raise ValueError('未知生产阶段')
    folder = Path(folder).resolve()
    hold = editorial_hold(folder)
    if hold:
        return ['编辑暂停交付：' + hold['reason']]
    try:
        now = now or datetime.now(timezone.utc)
        # Admission is necessary, never a substitute for the user approvals below.
        from scripts.topic_evidence import assess
        topic_path = confined_file(folder, 'review/topic_evidence.json')
        topic_card = json.loads(topic_path.read_text(encoding='utf-8'))
        admission = assess(topic_card, folder, now)
        if not admission.get('ready_for_topic_review'):
            details = admission.get('errors', []) + admission.get('engagement_errors', [])
            raise ValueError('选题证据未过门槛，当前为 ' + admission['state'] + '：' + '；'.join(details))
        brief = json.loads(confined_file(folder, ARTIFACTS['topic']).read_text(encoding='utf-8'))
        if brief.get('topic_evidence_sha256') != digest(topic_path):
            raise ValueError('审题简报未绑定当前五要素及接话口版本，不能沿用旧批准')
        record = json.loads((folder/'production_review.json').read_text(encoding='utf-8'))
        if record.get('schema') != 1 or record.get('workflow') not in ALLOWED_WORKFLOWS:
            raise ValueError('缺少现行生产阶段记录，旧批准不能自动迁移')
        from scripts.editorial_policy import policy_sha256
        if record.get('policy_sha256') != policy_sha256():
            raise ValueError('阶段记录未绑定现行编辑政策')
        for key in ('reader_question', 'why_now', 'new_finding', 'difference_from_sources'):
            if not isinstance(brief.get(key), str) or not brief[key].strip():
                raise ValueError('选题简报缺少 ' + key)
        mode = brief.get('mode')
        if mode not in ('news', 'explainer'):
            raise ValueError('须明确新闻或背景解释稿；不能自动换模式规避时效')
        sources = brief.get('news_sources', [])
        if not isinstance(sources, list) or (mode == 'news' and not sources):
            raise ValueError('新闻稿缺少近期原文证据')
        from scripts.radar.news_date import parse_article_date, classify_freshness, beijing_date
        if mode == 'news':
            for source in sources:
                if not isinstance(source, dict) or not str(source.get('url', '')).startswith('https://'):
                    raise ValueError('近期信源缺少HTTPS原文地址')
                raw = _snapshot(folder, source).read_text(encoding='utf-8')
                publication = parse_article_date(raw)
                if (not publication['found'] or publication['date'] != beijing_date(source.get('published_at'))
                        or classify_freshness(publication['date'], now) != 'fresh'):
                    raise ValueError('新闻原文日期不在北京T-0至T-4或缺乏可信发布日期；旧数据不能冒充现状')
        for name in ASSESSMENTS:
            assessment = record.get('assessments', {}).get(name, {})
            if (assessment.get('status') != 'reviewed'
                    or not str(assessment.get('finding', '')).strip()):
                raise ValueError('未完成具体编辑判断：' + name)
            _snapshot(folder, assessment)
        if record.get('unresolved_blockers') != []:
            raise ValueError('原创增量、时效或内容风险仍有阻断，不能以润色代替补证')
        previous = None
        required = ('topic',) if stage == 'topic' else ('topic', 'sample') if stage == 'sample' else ('topic', 'sample', 'manuscript')
        for name in required:
            previous = _approval(folder, record.get('approvals', {}).get(name), name, previous, now)
        return []
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return ['生产阶段阻断：' + str(exc)]


def review_files(folder, stage='delivery'):
    """Dependencies that must travel with the approved source to the write boundary."""
    folder = Path(folder).resolve()
    errors = review_errors(folder, stage)
    if errors:
        raise ValueError('; '.join(errors))
    record = json.loads((folder/'production_review.json').read_text(encoding='utf-8'))
    brief = json.loads((folder/ARTIFACTS['topic']).read_text(encoding='utf-8'))
    from scripts.topic_evidence import assess
    card_path = folder/'review/topic_evidence.json'
    admission = assess(json.loads(card_path.read_text(encoding='utf-8')), folder)
    required = ('topic',) if stage == 'topic' else ('topic', 'sample') if stage == 'sample' else tuple(ARTIFACTS)
    result = [folder/'production_review.json', card_path, *(folder/ARTIFACTS[name] for name in required)]
    result += [confined_file(folder, relative) for relative in admission['evidence_files']]
    entries = [*(record['approvals'][name] for name in required), *record['assessments'].values()]
    if brief['mode'] == 'news':
        entries += brief['news_sources']
    result += [_snapshot(folder, entry) for entry in entries]
    return list(dict.fromkeys(result))


def main():
    parser = argparse.ArgumentParser(description='只读核对用户审题、试写和全文认可；不产生批准')
    parser.add_argument('--dir', required=True)
    parser.add_argument('--stage', choices=['topic', 'sample', 'delivery'], default='delivery')
    args = parser.parse_args()
    errors = review_errors(args.dir, args.stage)
    print(json.dumps({'workflow': WORKFLOW, 'stage': args.stage, 'passed': not errors,
                      'errors': errors, 'scope': 'retained_records_not_identity_or_platform_certification'}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
