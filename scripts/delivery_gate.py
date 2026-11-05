"""Local delivery checks shared by the CLI; remote worker rechecks final HTML."""
import json
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from PIL import Image
from scripts.article_identity import canonical_url
from scripts.content_contract import prepare, render_article, template_manifest_errors
from tools.jackie_studio.engine.audit_gate import audit, RANGES
from scripts.content_images import iter_content_images
from tools.jackie_studio.engine.history import published_images

class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []
    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            source = dict(attrs).get('src')
            if not source: raise ValueError('图片缺少 src')
            self.sources.append(source)

def render_matches(html, content, track, image_urls, industry):
    # Uploading adds a CDN cache without changing the approved local build.
    # Require a complete exact render in either supported asset representation.
    return (html == render_article(content, track, {}, industry)
            or html == render_article(content, track, image_urls, industry))


def content_image_errors(folder, content, sources, image_urls=None):
    """Validate occurrence count/order and physical dimensions, including lead."""
    folder = Path(folder).resolve()
    errors = []
    track = content.get('content_track')
    solar = track == 'F' and content.get('layout_profile') == 'solar_editorial'
    if track in ('A', 'D', 'F') and not solar and not content.get('lead_image'):
        errors.append('正文缺少开篇导语图片 lead_image')
    images = list(iter_content_images(content))
    keys = [image['key'] for _, image in images]
    mapped = [(image_urls or {}).get(key, key) for key in keys]
    if len(sources) != len(keys):
        errors.append('HTML 配图与内容图注数量不一致')
    elif sources != keys and sources != mapped:
        errors.append('HTML 配图顺序与内容插槽不一致')
    for slot, image in images:
        path = (folder / image['key']).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            errors.append('正文图片不存在或路径越界：' + image['key'])
            continue
        try:
            with Image.open(path) as physical:
                if track in ('A', 'D', 'F') and physical.width <= physical.height:
                    errors.append('正文配图须为横图（宽度 > 高度）：' + slot)
                for field, actual in (('width', physical.width), ('height', physical.height)):
                    if field in image and (type(image[field]) is not int or image[field] != actual):
                        errors.append('正文配图声明尺寸与实际不一致：' + slot + '.' + field)
        except (OSError, ValueError) as exc:
            errors.append('正文图片读取失败：' + slot + '：' + str(exc))
    return errors


def delivery_history_errors(paths, historical, coverage):
    """Release rechecks full coverage and the strict >30 history distance."""
    from scripts.screen_candidate_images import _validate_history
    from scripts.check_image_reuse import dhash, ham
    _validate_history(historical, coverage)
    errors = []
    for path in paths:
        digest = dhash(path, n=16)
        distance, closest = min((ham(digest, old), name) for name, old in historical)
        if distance <= 30:
            errors.append(f'命中已发布配图：{closest}（距离 {distance} ≤ 30），必须更换')
    return errors


def check(folder, track, industry):
    folder = Path(folder).resolve()
    errors = []
    if track == 'B':
        from scripts.gallery.contract import check as gallery_check
        return gallery_check(folder, industry)
    try:
        content = prepare(json.loads((folder/'content.json').read_text(encoding='utf-8')), track, industry)
        from scripts.preflight_compliance import audit as compliance_audit
        compliance = compliance_audit(folder, content, track)
        errors.extend(compliance['errors'])
        if track == 'C' and content['resource_card']:
            from scripts.release_contract import verify_manifest
            root = Path(__file__).resolve().parents[1]
            reference = content['resource_card'].get('release_manifest')
            if not reference: raise ValueError('C 轨资源卡必须绑定已验收的 release_manifest')
            manifest_path = (root/reference).resolve()
            if not manifest_path.is_relative_to(root/'release_packs'):
                raise ValueError('资源清单必须位于本项目 release_packs')
            manifest = verify_manifest(manifest_path)
            if manifest['product_level'] == 'demo':
                if '演示骨架' not in content['resource_card']['desc']:
                    raise ValueError('演示资源卡必须明确说明演示骨架')
            elif not manifest.get('production_ready'):
                raise ValueError('资源包未完成功能验收')
        html = (folder/'article.html').read_text(encoding='utf-8')
        manifest = json.loads((folder/'build_manifest.json').read_text(encoding='utf-8'))
        from scripts.review_contract import review_errors, verify_build_hash
        verify_build_hash(manifest)
        errors.extend(review_errors(folder, manifest['build_hash'], manifest['assets']))
        from scripts.editorial_policy import writing_review_errors
        errors.extend(writing_review_errors(folder))
        errors.extend(template_manifest_errors(manifest, content, track, industry))
        expected = {'compliance_rules_sha256': hashlib.sha256(Path(__file__).with_name('preflight_compliance.py').read_bytes()).hexdigest(),
                    'editorial_policy_sha256': hashlib.sha256(Path(__file__).resolve().parents[1].joinpath('references/editorial_policy.json').read_bytes()).hexdigest(),
                    'writing_gate_sha256': hashlib.sha256(Path(__file__).with_name('editorial_policy.py').read_bytes()).hexdigest(),
                    'topic_spec_sha256': hashlib.sha256((folder/'topic_spec.json').read_bytes()).hexdigest() if (folder/'topic_spec.json').exists() else None,
                    'content_sha256': hashlib.sha256((folder/'content.json').read_bytes()).hexdigest(),
                    'renderer_sha256': hashlib.sha256(Path(__file__).with_name('content_contract.py').read_bytes()).hexdigest(),
                    'html_sha256': hashlib.sha256(html.encode('utf-8')).hexdigest()}
        if manifest.get('content_track') != track or manifest.get('industry') != industry or any(manifest.get(k) != v for k, v in expected.items()):
            errors.append('构建清单与当前输入不一致，请重新 render')
        for asset in manifest.get('assets', []):
            path = (folder/asset['path']).resolve()
            if not path.is_relative_to(folder) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
                errors.append('构建后配图发生变化，请重新 render')
        cache = folder/'cdn_urls.json'
        image_urls = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
        if not render_matches(html, content, track, image_urls, industry):
            errors.append('HTML 与当前内容/模板不一致，请重新执行 render')
        parser = Assets(); parser.feed(html)
        if not parser.sources: errors.append('正文没有配图')
        paths = []
        # Verify the original assets even if article HTML already uses CDN URLs.
        originals = [image['key'] for _, image in iter_content_images(content)]
        errors.extend(content_image_errors(folder, content, parser.sources, image_urls))
        if {a['path'] for a in manifest.get('assets', [])} != set(originals + ['cover_900x383.jpg']):
            errors.append('构建清单未完整绑定正文配图与标准封面')
        if track == 'A':
            errors.extend(track_a_assets(folder, originals))
        if len(parser.sources) != len(originals): errors.append('HTML 配图与内容图注数量不一致')
        for src in dict.fromkeys(originals + [s for s in parser.sources if not re.match(r'^https?://(?:mmbiz|sz_mmbiz)\.qpic\.cn/', s)]):
            path = (folder/src).resolve()
            if '://' in src or not path.is_relative_to(folder) or not path.is_file():
                errors.append('正文图片不存在或路径越界：' + src)
            else: paths.append(path)
        cover = folder/'cover_900x383.jpg'
        with Image.open(cover) as image:
            if image.size != (900, 383): errors.append('封面实际尺寸必须为 900×383')
        # Check cover against history, but not against its own uncropped source.
        historical, coverage = published_images()
        errors.extend(delivery_history_errors(paths + [cover], historical, coverage))
        if not RANGES[track][2] <= len(html) <= RANGES[track][3]:
            errors.append('本地渲染 HTML 长度不符合轨道要求')
        result = audit(html, 'html', html, track, paths, historical=historical, history_coverage=coverage)
        cover_result = audit(html, 'html', html, track, [cover], historical=historical)
        errors.extend(result['errors']); errors.extend(cover_result['errors'])
        registry = json.loads((Path(__file__).resolve().parents[1]/'references/published_articles.json').read_text(encoding='utf-8'))
        published = {canonical_url(a['url']): a for a in registry if a.get('status') == 'published'}
        for rec in content['recommendations']:
            entry = published.get(canonical_url(rec['url']))
            if not entry or entry.get('track') != industry:
                errors.append('承接链接未发表或跨产业：' + rec['title'])
        return {**result, 'passed': not errors, 'errors': list(dict.fromkeys(errors)), 'history_coverage': coverage, 'compliance': compliance}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {'passed': False, 'errors': [str(exc)]}


def track_a_assets(folder, originals):
    """Enforce editorial asset inventory, not guessed filename categories."""
    errors=[]
    try:
        review=json.loads((folder/'editorial_review.json').read_text(encoding='utf-8'))
        entries=review.get('images',{})
        photographs=[];cards=[];motion=[]
        for key in dict.fromkeys(originals):
            entry=entries.get(key,{})
            kind=entry.get('asset_kind')
            path=folder/key
            with Image.open(path) as image:
                if kind=='documentary_photo':
                    photographs.append(key)
                    if image.width <= image.height: errors.append('A轨纪实照片须为横图：'+key)
                elif kind=='data_card':
                    cards.append(key)
                    if image.size!=(2000,1000) or image.convert('RGB').getpixel((0,0))!=(250,249,246):
                        errors.append('A轨数据卡须2000×1000及#faf9f6暖白底：'+key)
                elif kind=='documentary_gif':
                    motion.append(key)
                    if image.format!='GIF' or getattr(image,'n_frames',1)<2 or not entry.get('motion_source'):
                        errors.append('A轨GIF须为多帧且绑定原始纪实视频来源：'+key)
                else: errors.append('A轨配图缺少可信类型声明 asset_kind：'+key)
        # 🔴 Codex 第三轮 B06：纪实实拍下限改为从唯一注册表取，不再硬编码 2。
        try:
            from track_policy import min_photographs_for as _minphoto
            _need = _minphoto('A') or 0
        except Exception:
            _need = 2
        if len(photographs) < _need:
            errors.append('A轨须至少 %d 张纪实横图' % _need)
        if len(cards) > 5: errors.append('A轨数据卡不宜超过 5 张')
    except (OSError,ValueError,TypeError) as exc: errors.append('A轨资产审计失败：'+str(exc))
    return errors
