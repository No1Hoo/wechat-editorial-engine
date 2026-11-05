"""Executable editorial policy and evidence-bound writing review.

Signals identify explicit retired scaffolding, not an AI detector or factual verdict.
A passing review validates its retained record; it does not certify human authorship.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / 'references' / 'editorial_policy.json'
REVIEW_ASPECTS = ('claims', 'structure', 'language', 'ending')


def policy():
    value = json.loads(POLICY_PATH.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not value.get('version') or not value.get('principles'):
        raise ValueError('写作政策缺失或损坏，禁止使用旧提示词后备')
    return value


def policy_sha256():
    return hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()


def system_prompt(action):
    value = policy()
    task = value['actions'].get(action, value['actions']['humanize'])
    return ('你是Jackie观察视角的中文内容编辑。现行写作政策：' + value['version'] + '\n'
            + '\n'.join('- ' + p for p in value['principles']) + '\n本次任务：' + task)


def plain(value):
    return unescape(re.sub(r'<[^>]*>', '', str(value or ''))).strip()


def passages(content):
    """Enumerate visible template fields, excluding internal evidence and narration."""
    def walk(value, path):
        if isinstance(value, str):
            yield path, plain(value)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                yield from walk(item, f'{path}/{i}')
        elif isinstance(value, dict):
            for name, item in value.items():
                if name in ('title', 'label', 'desc', 'name', 'content', 'caption',
                            'source_title', 'quote_text', 'items'):
                    yield from walk(item, f'{path}/{name}')
    for key in ('title', 'summary', 'digest', 'lead_quote', 'lead_hook', 'x_lead_hook',
                'hook_quote', 'comparison_card', 'decision_section_title', 'decision_intro',
                'key_takeaways', 'decision_guides', 'benchmarks', 'industry_toolkit',
                'recommendation_title', 'recommendations', 'copyright_disclaimer', 'lead_image'):
        yield from walk(content.get(key), key)
    if content.get('show_references'):
        yield from walk(content.get('references'), 'references')
    for i, section in enumerate(content.get('sections', [])):
        if not isinstance(section, dict):
            continue
        for key in ('title', 'golden_quote', 'jackie_observation', 'paragraphs',
                    'extra_paragraphs', 'extra_paragraphs_after_image',
                    'extra_paragraphs_after_callout', 'callout', 'highlight_box',
                    'quote_card', 'items_box', 'image', 'images', 'extra_image', 'final_card', 'gif_or_image'):
            yield from walk(section.get(key), f'sections/{i}/{key}')


def scan(content):
    if not isinstance(content, dict): raise ValueError('写作扫描需要内容对象')
    errors, warnings = [], []
    records = list(passages(content))
    # Internal writing instructions and textbook outline headings blocked from reader-facing titles.
    retired_headings = re.compile(
        r'Jackie\s*算硬账|跨国泥泞现场|消费神话下的原料饥渴|残酷的.{0,8}账本|最残酷的.{0,12}硬账|大宗定格'
        r'|从.{1,15}说起|不神化.{1,10}也不否定|理性看待.{1,12}'
    )
    retired_slogans = (
        '油脂分子细致入微的残酷驯服',
        '失重与腐烂的物理重压下崩断',
        '为现代产业留一份清醒的观察坐标',
        '朝着更加健康与理性的方向稳步前行',
        '推开了一扇更具价值的大门',
        '不相信空洞的算力童话',
    )
    chinese_stat_numbers = re.compile(
        r'百分之[零一二三四五六七八九十百点]+|二〇二[0-9零一二三四五六七八九]年|零点[一二三四五六七八九]+'
    )
    sections = [s for s in content.get('sections', []) if isinstance(s, dict)]
    is_full_manuscript = len(sections) >= 4
    for location, text in records:
        if location.endswith('/caption'):
            # R11（2026-10-29）：Rule 12「图注末尾严禁句号」此前没有任何代码实现。
            # 旧逻辑只判断「拆句后是否 >1 句」，于是「华中某县法院审判法庭外景。」
            # 这种单句带句号会被静默放行。现在改为按末尾字符硬阻断。
            stripped = text.rstrip()
            if len([s for s in re.split(r'[。！？!?]+', stripped) if s.strip()]) > 1:
                errors.append('图注只留一句场景说明：' + location)
            if stripped.endswith(('。', '！', '？', '.', '!', '?')):
                errors.append('图注末尾严禁句号/感叹号/问号（Rule 12）：' + location)
            if not stripped:
                errors.append('图注为空：' + location)
            elif len(stripped) > 40:
                warnings.append('图注偏长（%d 字，建议 10~22 字单句场景说明）：%s'
                                % (len(stripped), location))
            if re.search(r'许可[：:]|照片[：:]|CC BY|creativecommons\.org', text, re.I):
                errors.append('图注禁夹带署名许可，须集中另列：' + location)
        if (location == 'title' or location.endswith('/title')) and retired_headings.search(text):
            errors.append(f'旧写作栏目或教科书提纲标题回流：{location}「{text[:85]}」；请按本篇商业悬念或利益冲突重新组织')
        for phrase in retired_slogans:
            if phrase in text:
                errors.append(f'已退役的模板或晚会式升华结语：{location}「{phrase}」')
        m_num = chinese_stat_numbers.search(text)
        if m_num:
            errors.append(f'汉字数字流水账（须改用阿拉伯数字与百分比符号）：{location}「{m_num.group(0)}」')
        if is_full_manuscript and re.search(r'^sections/\d+/(extra_)?paragraphs', location) and len(text) > 165:
            errors.append(f'段落过长（{len(text)}字 > 165字上限），须按手机端2-4行呼吸节奏拆段：{location}')
    if is_full_manuscript:
        raw_json = json.dumps(content, ensure_ascii=False)
        has_anchor = bool(
            content.get('key_takeaways')
            or content.get('comparison_card')
            or '<strong>' in raw_json
            or any(
                s.get('items_box') or s.get('quote_card') or s.get('callout')
                or s.get('highlight_box') or s.get('golden_quote')
                for s in sections
            )
        )
        if not has_anchor:
            errors.append('全文缺少杂志级视觉锚点（须至少配置1处要点卡片/数据看板/金句引用或重点加粗，禁止裸奔灰字长文）')
    joined = '\n'.join(text for _, text in records)
    terms = ('残酷', '底层逻辑', '物理极限', '神话', '泥泞', '底牌')
    found = {term: joined.count(term) for term in terms if term in joined}
    if sum(found.values()) >= 5:
        warnings.append('抽象/戏剧化修辞较密集，请逐句检查证据增量：' + json.dumps(found, ensure_ascii=False))
    return {'passed': not errors, 'errors': list(dict.fromkeys(errors)), 'warnings': warnings,
            'scope': 'retired_scaffolding_signals_not_originality_or_fact_certification', 'policy_sha256': policy_sha256()}


def writing_review_errors(folder):
    """Required for every new write, independent of model-declared narration metadata."""
    folder = Path(folder).resolve()
    try:
        source = (folder / 'content.json').read_bytes()
        content = json.loads(source)
        result = scan(content)
        errors = list(result['errors'])
        from scripts.production_review import review_errors as production_errors
        errors.extend(production_errors(folder))
        review = json.loads((folder / 'writing_review.json').read_text(encoding='utf-8'))
        if not isinstance(review, dict): raise ValueError('writing_review须为对象')
        if review.get('schema') != 1 or review.get('policy_sha256') != policy_sha256():
            errors.append('正文编辑复核未绑定当前写作政策')
        if review.get('content_sha256') != hashlib.sha256(source).hexdigest():
            errors.append('正文编辑复核已失效：当前源稿已改变')
        if review.get('status') != 'ready_for_draft' or not review.get('reviewer') or not review.get('reviewer_identity'):
            errors.append('缺少真实编辑身份、范围或ready_for_draft结论；不得冒充人类/平台批准')
        if review.get('reviewer_type') not in ('ai_assisted', 'human'):
            errors.append('必须明确编辑复核为ai_assisted或human')
        at = datetime.fromisoformat(str(review.get('reviewed_at', '')).replace('Z', '+00:00'))
        if at.tzinfo is None or at > datetime.now(timezone.utc): errors.append('正文编辑复核时间缺时区或位于未来')
        from scripts.review_contract import snapshot_errors
        errors.extend(snapshot_errors(folder, review, '正文编辑复核'))
        visible = dict(passages(content))
        checks = review.get('checks', {})
        if not isinstance(checks, dict): checks = {}
        for aspect in REVIEW_ASPECTS:
            item = checks.get(aspect, {})
            if not isinstance(item, dict) or item.get('status') != 'reviewed' or not str(item.get('finding', '')).strip():
                errors.append('正文编辑复核缺少具体判断：' + aspect)
                continue
            quote = item.get('quote')
            if not isinstance(quote, str) or len(plain(quote)) < 8 or plain(quote) not in visible.get(item.get('passage'), ''):
                errors.append('编辑复核引用未定位到当前正文：' + aspect)
        issues = review.get('unresolved_blockers')
        if not isinstance(issues, list) or issues: errors.append('正文编辑仍有阻断项，禁止交付')
        return list(dict.fromkeys(errors))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return ['缺少有效且绑定当前版本的writing_review.json：' + str(exc)]


def submitted_review_errors(folder, payload):
    """Reject a reviewed source paired with a different submitted article."""
    errors = writing_review_errors(folder)
    if errors:
        return errors
    try:
        content = json.loads((Path(folder) / 'content.json').read_text(encoding='utf-8'))
        from scripts.content_contract import render_article
        context = dict(content)
        context.pop('img_urls', None)
        title = context.get('decision_section_title')
        if isinstance(title, str) and title.strip():
            context['decision_section_title'] = re.sub(r'^\s*0?\d{1,2}\s*[.、．]\s*', '', title).strip()
        expected = render_article(context, content.get('content_track'),
                                  industry=content.get('industry') or content.get('track'))
        if payload.get('title') != content.get('title'):
            errors.append('实际提交标题与已复核源稿不一致')
        if plain(payload.get('content')) != plain(expected):
            errors.append('实际提交正文与已复核源稿的受控渲染不一致')
        return errors
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return ['无法核对实际提交正文与已复核源稿：' + str(exc)]
