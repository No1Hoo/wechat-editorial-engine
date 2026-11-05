"""Editorial approvals bind to immutable build inputs, never to a filename."""
import hashlib
import json
from pathlib import Path


def review_errors(folder, build_hash, assets):
    path = Path(folder) / 'editorial_review.json'
    if not path.is_file():
        return ['缺少 editorial_review.json：事实、法务与图片来源待审核']
    try:
        review = json.loads(path.read_text(encoding='utf-8'))
        if review.get('build_hash') != build_hash:
            return ['审核摘要已失效：内容或资产改变后必须重新审核']
        errors = []
        if review.get('schema') != 2:
            errors.append('审核记录须升级为schema=2并保留可核验的原始证据快照；旧批准不可直接沿用')
        for name in ('facts', 'legal'):
            entry = review.get(name, {})
            if (entry.get('status') != 'approved' or not entry.get('reviewer')
                    or not entry.get('reviewed_at') or not entry.get('evidence')):
                errors.append(name + ' 缺少批准人、时间或证据')
            if entry.get('status') == 'approved' and not entry.get('reviewer_identity'):
                errors.append(name + ' 缺少真实审核身份及授权范围声明')
            for evidence in entry.get('evidence', []):
                if not isinstance(evidence, dict) or not evidence.get('source') or not evidence.get('finding'):
                    errors.append(name + ' 证据必须包含来源与核验结论')
                elif entry.get('status') == 'approved':
                    errors.extend(snapshot_errors(folder, evidence, name))
        reviewed = review.get('images', {})
        for asset in assets:
            entry = reviewed.get(asset['path'], {})
            if (entry.get('sha256') != asset['sha256'] or entry.get('status') != 'approved'
                    or not entry.get('source') or not entry.get('rights')
                    or not entry.get('visual_observation') or not entry.get('caption_binding')
                    or not entry.get('reviewer') or not entry.get('reviewed_at')):
                errors.append('图片来源/视觉/图文审核缺失或失效：' + asset['path'])
            elif entry.get('status') == 'approved':
                errors.extend(snapshot_errors(folder, entry, asset['path']))
        return list(dict.fromkeys(errors))
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        return ['审核记录格式错误：' + str(exc)]


def verify_build_hash(manifest):
    payload = {k: v for k, v in manifest.items() if k not in ('build_hash', 'created_at')}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if manifest.get('build_hash') != digest:
        raise ValueError('构建摘要不一致，禁止修改清单后沿用回执')


def snapshot_errors(folder, entry, label):
    """Bind retained evidence bytes; this does not certify the truth of a claim."""
    root=Path(folder).resolve()
    try:
        relative=entry.get('evidence_file')
        expected=entry.get('evidence_sha256')
        if not relative or not expected: return [label+' 缺少证据快照路径及SHA256']
        path=(root/relative).resolve()
        if not path.is_relative_to(root) or not path.is_file(): return [label+' 证据快照不存在或越界']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected: return [label+' 证据快照摘要不匹配']
        return []
    except (OSError,TypeError,ValueError): return [label+' 证据快照无效']
