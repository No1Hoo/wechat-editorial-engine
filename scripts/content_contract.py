"""Two-axis content contract and a narrow compatibility layer for old emphasis."""
import copy
import re
from pathlib import Path
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit
from markupsafe import Markup

# 轨道白名单来自唯一事实源，避免与 pipeline/worker 各写一套（JOS-012）。
try:
    from scripts.track_policy import active_tracks as _active_tracks
    TRACKS = set(_active_tracks())
except Exception:  # 独立运行（非包内导入）时的最小回退
    TRACKS = {'A', 'D', 'F'}
INDUSTRIES = {'fishery', 'planting', 'livestock', 'agri-tech'}

# 兼容模式下降级（剥离标签保留文本）的标签记录。
# 每次 inline() 调用会重置，供 prepare() 汇总到 _contract_warnings。
_INLINE_DEGRADED = []

class InlineParser(HTMLParser):
    ALLOWED = {'strong', 'b', 'em', 'br', 'a'}
    # 🔴 Codex JOS-034：存量稿件里存在带 style 的 <span>（用于右对齐署名/高亮），
    #    严格模式下会直接拒绝，导致 6 篇旧稿完全无法渲染。
    #    兼容策略：**只对 span** 做「剥离标签、保留内部文本」，
    #    绝不保留其 style 属性（样式仍由模板决定），并记入 degraded 列表留痕。
    DEGRADABLE = {'span'}

    def __init__(self, compat=False):
        super().__init__(convert_charrefs=True)
        self.output, self.stack = [], []
        self.compat = compat
        self.degraded = []          # 记录被降级处理的标签
        self._skip_depth = []       # span 嵌套深度：>0 表示当前在剥离区内

    def _degrading(self):
        return any(self._skip_depth)

    def handle_starttag(self, tag, attrs):
        if tag in self.DEGRADABLE and self.compat:
            self.degraded.append(tag)
            if tag != 'br':
                self._skip_depth.append(tag)
            return
        if tag not in self.ALLOWED:
            raise ValueError('内容包含未授权 HTML 标签：' + tag)
        if tag == 'a':
            if len(attrs) != 1 or attrs[0][0] != 'href':
                raise ValueError('链接只允许 href')
            url = attrs[0][1]
            if not url or urlsplit(url).scheme != 'https' or not urlsplit(url).hostname:
                raise ValueError('链接必须使用 HTTPS')
            self.output.append('<a href="' + escape(url, quote=True) + '">')
        else:
            if attrs:
                raise ValueError('内容不能指定样式或 HTML 属性')
            self.output.append('<' + tag + '>')
        if tag != 'br':
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.DEGRADABLE and self.compat and self._skip_depth:
            self._skip_depth.pop()
            return
        if not self.stack or self.stack.pop() != tag:
            raise ValueError('富文本标签未正确闭合')
        self.output.append('</' + tag + '>')

    def handle_data(self, data):
        # 在剥离区内仍保留纯文本（丢掉样式，但不丢内容）
        self.output.append(escape(data, quote=False))

    def handle_startendtag(self, tag, attrs):
        if tag != 'br': raise ValueError('只允许 br 自闭合标签')
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.output.append(escape(data))

    def handle_comment(self, data):
        raise ValueError('内容不能包含 HTML 注释')

    def handle_decl(self, decl):
        raise ValueError('内容不能包含 HTML 声明')

def inline(value, compat=False):
    """New inputs are nodes; legacy strings may contain attribute-free emphasis.

    🔴 默认必须是**严格模式**（compat=False）：`inline()` 是公开的安全边界，
    调用方默认期望它拒绝一切带样式/属性的标签（契约测试亦如此断言）。
    兼容模式（compat=True）只由 `prepare()` 在渲染存量稿时显式开启，
    且仅对 <span> 做「剥离标签保留文本」。
    """
    _INLINE_DEGRADED.clear()
    if isinstance(value, list):
        output = []
        for node in value:
            if not isinstance(node, dict) or set(node) - {'type', 'text', 'url'}:
                raise ValueError('非法富文本节点')
            kind, text = node.get('type'), node.get('text')
            if not isinstance(text, str):
                raise ValueError('富文本节点缺少 text')
            text = escape(text)
            if kind == 'text': output.append(text)
            elif kind in {'strong', 'em'}: output.append(f'<{kind}>{text}</{kind}>')
            elif kind == 'link':
                url = node.get('url', '')
                if urlsplit(url).scheme != 'https' or not urlsplit(url).hostname:
                    raise ValueError('链接必须使用 HTTPS')
                output.append(f'<a href="{escape(url, quote=True)}">{text}</a>')
            else: raise ValueError('未知富文本节点类型')
        return Markup(''.join(output))
    if not isinstance(value, str):
        raise ValueError('正文必须是文本或节点列表')
    parser = InlineParser(compat=compat)
    parser.feed(value)
    parser.close()
    if parser.stack:
        raise ValueError('富文本标签未闭合')
    if parser.degraded:
        _INLINE_DEGRADED.append(parser.degraded)
    return Markup(''.join(parser.output))

def _check_narration(data, warnings_list):
    """校验可选的 `narration` 字段（C05 决策：宽松校验，只记录不阻断）。

    设计原则（2026-10-19 内容改造）：
      - 字段**缺失**是合法的：旧稿没有这个字段，必须按旧行为渲染。
      - 校验**只产生警告，绝不阻断渲染**——它不是门禁，是记录。
      - 未知值一律警告并忽略，避免把编辑笔误变成渲染失败。

    结构：
        "narration": {
            "methods": ["案例对照", "技术解释"],
            "primary": "案例对照",
            "presentation": "B",
            "note": "为什么选这个方法（可选）"
        }
    """
    narration = data.get('narration')
    if narration is None:
        return  # 旧稿常态，不警告
    if not isinstance(narration, dict):
        warnings_list.append('narration 不是对象，已忽略（不影响渲染）')
        return

    methods = narration.get('methods')
    if methods is None:
        warnings_list.append('narration.methods 缺失（可选字段，不影响渲染）')
    elif not isinstance(methods, list):
        warnings_list.append('narration.methods 不是数组，已忽略')
    elif not methods:
        warnings_list.append('narration.methods 为空数组（建议至少填 1 项）')

    primary = narration.get('primary')
    if primary is not None:
        if not isinstance(primary, str):
            warnings_list.append('narration.primary 不是字符串，已忽略')
        elif isinstance(methods, list) and methods and primary not in methods:
            warnings_list.append(
                'narration.primary=%r 不在 methods 中（记录不一致，不影响渲染）'
                % primary)

    presentation = narration.get('presentation')
    if presentation is not None and presentation not in ('A', 'B', 'C'):
        warnings_list.append(
            'narration.presentation=%r 不是 A/B/C，已忽略' % (presentation,))


def prepare(data, track, industry=None):
    if track not in TRACKS or not isinstance(data, dict):
        raise ValueError('非法内容轨道或数据类型')
    # 契约层的「已处理但不阻断」记录（如剥离 callout 样式键）。
    # 这些不是错误，但必须留痕，便于排查「为什么样式没生效」。
    warnings_list = []
    # 🔴 C05（2026-10-19）：narration 是可选记录字段，只警告不阻断。
    _check_narration(data, warnings_list)
    result = copy.deepcopy(data)
    # 🔴 Codex 第三轮 B05：审批签名必须绑定「调用方提供的原始内容」，
    #    而不是 prepare() 补完默认值之后的结果——否则调用方无法复算，
    #    正例也会因为默认值差异而验签失败。
    _original_view = {k: copy.deepcopy(v) for k, v in result.items()
                      if k not in ('references_approval', 'signature_approval',
                                   '_approval_warnings', '_contract_warnings')}
    # 🔴 Codex JOS-034：旧写法 declared/sector 会「有就用、没有就悄悄回退到参数」，
    #    导致两类问题都无法区分：
    #      (a) 内容里声明了 A，调用方传了 D —— 真冲突，必须拒绝；
    #      (b) 内容里根本没有字段 —— 属存量元数据缺失，可由调用参数补全。
    #    这里把两者分开：显式声明且冲突才拒绝；缺失则补全并留痕。
    _declared_track = result.get('content_track')
    _declared_sector = result.get('industry', result.get('track'))
    if _declared_track is not None and _declared_track != track:
        raise ValueError(
            '内容轨道声明冲突：content.json 写 %r，调用参数为 %r'
            % (_declared_track, track))
    if _declared_sector is not None and _declared_sector not in INDUSTRIES:
        raise ValueError(
            '内容产业声明非法：%r 不在 %s 中' % (_declared_sector, sorted(INDUSTRIES)))
    if _declared_sector is not None and industry and _declared_sector != industry:
        raise ValueError(
            '内容产业声明冲突：content.json 写 %r，调用参数为 %r'
            % (_declared_sector, industry))
    if _declared_sector is None:
        if not industry:
            raise ValueError('内容未声明产业，且调用方也未提供 industry 参数')
        warnings_list.append('content.json 缺少 industry/track 字段，已按调用参数补全为 %r'
                             % industry)
    sector = _declared_sector or industry
    if not isinstance(result.get('title'), str) or not result['title'].strip():
        raise ValueError('缺少文章标题')
    result.update(content_track=track, industry=sector)
    if not isinstance(result.get('sections'), list) or not result['sections']:
        raise ValueError('缺少内容章节')
    # Validate all strings, including fields currently ignored by a template.
    # 🔴 Codex JOS-034：旧实现只「校验」不改写——模板用 `|safe` 输出原文，
    #    于是带 style 的 <span> 既通过了校验（兼容模式）又原样出现在成品里。
    #    现在改为「边校验边归一」：把文本清洗成安全 HTML 再写回 result，
    #    模板无论如何都拿不到原始 style。
    _degraded_total = []

    def validate(value, key=''):
        if isinstance(value, str) and key != 'code_block':
            # 渲染存量稿时显式开启兼容模式（只剥离 span 样式，保留文本）
            cleaned = str(inline(value, compat=True))
            if _INLINE_DEGRADED:
                _degraded_total.extend(_INLINE_DEGRADED[0])
            return cleaned
        if isinstance(value, dict):
            for k in list(value.keys()):
                value[k] = validate(value[k], k)
            return value
        if isinstance(value, list):
            for i, v in enumerate(value):
                value[i] = validate(v, key)
            return value
        return value

    result = validate(result)
    if _degraded_total:
        warnings_list.append(
            '已剥离 %d 处 <span> 样式标签（只保留文本，样式由模板决定）'
            % len(_degraded_total))
    # show_references / show_signature 默认关闭：军规 §1/§13 取缔文末文献与机械署名。
    # 🔴 Codex JOS-014：旧实现只要 content.json 写 true 就输出，等于调用方自证豁免。
    #    现在 true 只是「申请」，还必须有配套的审批凭据才会真正生效：
    #      - references_approval / signature_approval 必须是 dict；
    #      - 必须含非空 approved_by（人类标识）与带时区的 approved_at；
    #      - 缺任一项 → 开关被强制降为 False，并在 _approval_warnings 留痕。
    top_defaults = {'lead_quote': '', 'key_takeaways': [], 'decision_guides': [],
                    'benchmarks': [], 'references': [], 'industry_toolkit': None,
                    'recommendations': [], 'hook_quote': '', 'resource_card': None,
                    'x_lead_hook': '', 'comparison_card': '', 'sources': [],
                    'lead_hook': '', 'copyright_disclaimer': '', 'lead_image': None,
                    # R11（2026-10-29）：标题分轨与具名主体登记，供 Gate 19/20 读取。
                    'title_mode': 'analysis', 'title_claims': [], 'title_comparison': None,
                    'title_subject': '', 'title_conflict': '', 'named_entities': [],
                    'show_references': False, 'show_signature': False,
                    'references_approval': None, 'signature_approval': None,
                    # 2026-10-19 三套呈现：缺失/非法时一律回退 B，
                    # 保证旧稿（无 narration 字段）渲染字节不变。
                    'presentation': 'B'}
    for key, default in top_defaults.items(): result.setdefault(key, default)
    if result.get('lead_image'):
        if not isinstance(result['lead_image'], dict) or not result['lead_image'].get('key') or not result['lead_image'].get('caption'):
            raise ValueError('开篇导语图片 lead_image 必须指定路径与图注')

    # 三套呈现：只从 narration.presentation 取，且必须是 A/B/C。
    _nar = result.get('narration')
    if isinstance(_nar, dict):
        _pres = _nar.get('presentation')
        if _pres in ('A', 'B', 'C'):
            result['presentation'] = _pres

    def _approval_ok(kind):
        """校验审批凭据；返回 (是否放行, 拒绝原因)。

        🔴 Codex B05：旧实现只检查 approved_by / approved_at 的**格式**，
        任何人都能填「jackie + 一个合法时间」自我批准。
        现在追加两道硬约束：
          1) 必须带 signature，且由本机 HMAC 密钥签发；
          2) 签名必须绑定 content_sha256（内容版本），
             同 id 内容被换后旧签名失效。
        """
        proof = result.get('%s_approval' % kind)
        if not isinstance(proof, dict):
            return False, '%s_approval 缺失或不是对象' % kind
        approver = str(proof.get('approved_by') or '').strip()
        if not approver:
            return False, '%s_approval 缺少 approved_by（必须由人类署名）' % kind
        raw_at = proof.get('approved_at')
        if not raw_at:
            return False, '%s_approval 缺少 approved_at' % kind
        try:
            from datetime import datetime as _dt, timezone as _tz
            ts = _dt.fromisoformat(str(raw_at).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return False, '%s_approval 的 approved_at 不是合法 ISO 时间' % kind
        if ts.tzinfo is None:
            return False, '%s_approval 的 approved_at 必须带时区' % kind
        if ts > _dt.now(_tz.utc):
            return False, '%s_approval 的 approved_at 位于未来' % kind

        # ---- 追加：签名 + 内容版本绑定 ----
        import hashlib as _h
        import json as _json
        try:
            from scripts.approval_sign import verify_approval as _verify
        except Exception as _exc:
            return False, '签名校验模块不可用：%s' % _exc
        bound = str(proof.get('content_sha256') or '')
        # 🔴 Codex 第三轮 B05：旧实现只把 title/sections/references 纳入指纹，
        #    模板还会输出 x_lead_hook / hook_quote / lead_quote / industry_toolkit 等，
        #    改了这些字段旧批准依然有效。现在改为对**送进模板的全部字段**取指纹
        #    （排除审批凭据本身与内部留痕，避免自引用）。
        _content_for_bind = _json.dumps(
            _original_view, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'))
        actual_sha = _h.sha256(_content_for_bind.encode('utf-8')).hexdigest()
        if not bound:
            return False, ('%s_approval 未绑定内容版本（缺 content_sha256）；'
                           '恢复文献/署名需对当前稿件内容签名' % kind)
        if bound != actual_sha:
            return False, ('%s_approval 的内容版本已变（签名绑定 %s…，当前 %s…）；'
                           '内容修改后必须重新审批'
                           % (kind, bound[:12], actual_sha[:12]))
        sig_ok, sig_reason = _verify(
            '%s_restore' % kind, str(result.get('slug') or result.get('title') or ''),
            approver, str(raw_at), proof.get('signature'),
            content_sha256=bound, require_content_binding=True)
        if not sig_ok:
            return False, '%s_approval 签名校验失败：%s' % (kind, sig_reason)
        return True, ''

    warnings = []
    for flag, kind, label in (
            ('show_references', 'references', '文末参考文献'),
            ('show_signature', 'signature', '机械署名')):
        if result.get(flag):
            ok, reason = _approval_ok(kind)
            if not ok:
                result[flag] = False
                warnings.append('已拒绝恢复%s：%s' % (label, reason))
    result['_approval_warnings'] = warnings
    section_defaults = {'image': None, 'extra_image': None, 'final_card': None,
                        'extra_paragraphs_after_image': [], 'extra_paragraphs': [],
                        'extra_paragraphs_after_callout': [],
                        'quote_card': None, 'items_box': None, 'golden_quote': '',
                        'jackie_observation': '', 'gif_or_image': None, 'code_block': '',
                        'highlight_box': None, 'callout': None, 'images': []}
    for section in result['sections']:
        if not isinstance(section, dict) or not isinstance(section.get('title'), str):
            raise ValueError('章节缺少标题')
        if not isinstance(section.get('paragraphs'), list):
            raise ValueError('章节 paragraphs 必须是列表')
        if track == 'A' and not isinstance(section.get('num'), str):
            raise ValueError('A 轨章节缺少编号')
        for key, default in section_defaults.items(): section.setdefault(key, copy.deepcopy(default))
        required_nested = {'quote_card': ('source_title', 'quote_text'),
                           'highlight_box': ('title', 'content'), 'callout': ('title', 'content')}
        for key, fields in required_nested.items():
            value = section[key]
            if value and (not isinstance(value, dict) or any(not isinstance(value.get(f), str) for f in fields)):
                raise ValueError(key + ' 缺少必填文本字段')
        if section['items_box']:
            box = section['items_box']
            if not isinstance(box, dict) or not isinstance(box.get('items'), list):
                raise ValueError('items_box 缺少 items 列表')
            box.setdefault('title', '')
            if any(not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('desc'), str) for item in box['items']):
                raise ValueError('items_box 条目缺少名称或说明')
        for key in ('image', 'extra_image', 'final_card', 'gif_or_image'):
            image = section[key]
            if image is not None and (not isinstance(image, dict) or not image.get('key') or not image.get('caption')):
                raise ValueError('图片必须指定路径与图注')
        for image in section['images']:
            if not isinstance(image, dict) or not image.get('key') or not image.get('caption'):
                raise ValueError('图片必须指定路径与图注')
        if section['callout']:
            # 🔴 Codex JOS-034：这条规则本身是对的——callout 样式必须由模板决定，
            #    不能由 content.json 指定。但历史上大量旧稿写了 bg/border/color，
            #    直接抛错会让它们**全部无法渲染**。
            #    折中：把这些样式键**剥离**并记录到 _contract_warnings，
            #    既不让内容覆盖模板样式，也不阻断存量稿件。
            _style_keys = [k for k in ('bg', 'border', 'color', 'title_color')
                           if k in section['callout']]
            if _style_keys:
                for _k in _style_keys:
                    section['callout'].pop(_k, None)
                warnings_list.append(
                    '已剥离 section[%s] callout 的样式键 %s（样式由模板决定）'
                    % (section.get('num') or section.get('title', '?'), _style_keys))
    for rec in result['recommendations']:
        if not isinstance(rec, dict) or not rec.get('url') or not rec.get('title'):
            raise ValueError('承接条目缺少链接或标题')
        rec.setdefault('desc', '')
    if result['industry_toolkit']:
        if not isinstance(result['industry_toolkit'], dict) or not isinstance(result['industry_toolkit'].get('desc'), str):
            raise ValueError('工具包缺少说明')
    if result['resource_card']:
        if not isinstance(result['resource_card'], dict) or any(not isinstance(result['resource_card'].get(k), str) for k in ('desc', 'keyword')):
            raise ValueError('资源卡缺少说明或关键词')
    # 把契约层「已处理但不阻断」的记录挂回结果，便于调用方排查
    if warnings_list:
        result['_contract_warnings'] = warnings_list
    return result

def resolve_template(track, presentation=None):
    """统一的模板解析：渲染与 pipeline 共用，避免选择规则分叉。

    返回 (模板文件名, 绝对路径)。

    安全约束：
      - track 必须命中受控映射，否则抛错（不接受任意字符串拼路径）。
      - 未知编辑记录仍兼容默认 B；已识别但不支持的执行选择明确拒绝。
      - 缺失模板不回退，以免用户误以为所选呈现已生效。

    受控映射（唯一真源）：
      A → track_a_pres_a.html.j2
      C → track_a_pres_c.html.j2
      其它（含 B / 缺省）→ track_a.html.j2
    """
    track_key = str(track).lower()
    if track_key.upper() not in TRACKS:
        raise ValueError('非法轨道：%r' % (track,))

    if track_key != 'a' and presentation in ('A', 'C'):
        raise ValueError('轨道 %s 尚未支持呈现 %s，请选择 B；未执行回退' %
                         (track_key.upper(), presentation))

    suffix = ''
    # 能力表（V304）：目前只有 A 轨实现了 A/C 两种呈现。
    # D/F 请求 A/C 在公开渲染入口明确拒绝，不伪装成所选呈现已生效。
    if track_key == 'a':
        if presentation == 'A':
            suffix = '_pres_a'
        elif presentation == 'C':
            suffix = '_pres_c'

    name = 'track_%s%s.html.j2' % (track_key, suffix)
    path = Path(__file__).resolve().parents[1] / 'templates' / name
    if not path.is_file():
        raise ValueError('呈现模板缺失：%s；未执行回退' % name)
    return name, path


def template_fingerprint(data, track, industry=None):
    """Resolve exactly what render_article uses and bind all static Jinja dependencies."""
    import hashlib
    import jinja2
    from jinja2 import meta
    content = prepare(data, track, industry)
    name, path = resolve_template(track, content.get('presentation'))
    root = path.parent.resolve()
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(root)))
    dependencies = {}

    def visit(template_name):
        candidate = (root / template_name).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise ValueError('模板依赖不存在或越界：%s' % template_name)
        canonical = candidate.relative_to(root).as_posix()
        if canonical in dependencies:
            return
        raw = candidate.read_bytes()
        dependencies[canonical] = hashlib.sha256(raw).hexdigest()
        tree = env.parse(raw.decode('utf-8'))
        for reference in meta.find_referenced_templates(tree):
            if reference is None:
                raise ValueError('模板含动态依赖，无法绑定构建：%s' % canonical)
            visit(reference)

    visit(name)
    return {'template_name': name,
            'template_sha256': dependencies[name],
            'template_dependencies': dependencies}


def template_manifest_errors(manifest, data, track, industry=None):
    """Read old single-file manifests without rewriting approvals; new builds bind dependencies."""
    actual = template_fingerprint(data, track, industry)
    keys = ('template_sha256',)
    if manifest.get('manifest_schema', 1) == 2:
        keys = tuple(actual)
    elif manifest.get('manifest_schema', 1) != 1:
        return ['不支持的构建清单版本，请重新 render']
    elif 'template_name' in manifest:
        keys += ('template_name',)
    if 'template_dependencies' in manifest:
        keys = tuple(actual)
    elif len(actual['template_dependencies']) > 1:
        return ['旧构建未绑定模板依赖，请重新 render 并重新审核']
    return (['构建模板或依赖与当前输入不一致，请重新 render']
            if any(manifest.get(k) != actual[k] for k in keys) else [])


def render_article(data, track, image_urls=None, industry=None):
    from pathlib import Path
    import jinja2
    content = prepare(data, track, industry)
    if track == 'B': raise ValueError('B 轨不是 HTML 文章，请使用图库契约')
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).resolve().parents[1]/'templates')),
                             autoescape=True, undefined=jinja2.StrictUndefined)
    env.filters['inline'] = inline
    # 三套呈现：统一走 resolve_template，与 pipeline 的指纹计算共用同一规则。
    # V304 能力表：只有 A 轨支持 A/C 两种呈现。
    _pres = content.get('presentation')
    _tpl_name, _ = resolve_template(track, _pres)
    return env.get_template(_tpl_name).render(**content, img_urls=image_urls or {})
