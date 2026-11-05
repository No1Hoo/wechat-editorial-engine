# -*- coding: utf-8 -*-
"""微信草稿箱运维操作（服务端执行 · 批次 1）。

由 pipeline.py 通过 ssh 调用。所有输出为单行 JSON，便于本地解析。

子命令：
    dump    <media_id>              导出单篇草稿完整内容
    list                            列出全部草稿（含指纹）
    verify  <media_id> [--json]     独立回读断言
    diff    <type> <id_or_dir>      漂移检测（type=id|dir）
    push    <dir> <media_id>        本地覆盖线上
    audit                           全库风控体检

约定：
    block 级问题 → 退出码 1（可被 pipeline 捕获）
    warn  级问题 → 退出码 0，仅在输出中标记
"""
import hashlib
from pathlib import Path
import json
import os
import re
import sys
import urllib.request

_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_RUNTIME_ROOT))
sys.path.insert(0, str(_RUNTIME_ROOT / 'scripts'))

from scripts import wechat_api
from scripts.risk_scan import load_lexicon, scan, summarize, strip_tags

MARK = "<<<OPSJSON>>>"


def out(obj, code=0):
    sys.stdout.write(MARK + json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(code)


def sha(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def fetch_all(page_size=20, max_pages=50):
    """拉取全部草稿（JOS-027）。

    历史实现固定 offset=0/count=30，台账超过 30 篇后 audit/list 会静默漏扫。
    这里按 total_count 分页，并在接口报错时抛出，不再把错误当成空库。
    """
    tok = wechat_api.get_access_token()
    url = "https://api.weixin.qq.com/cgi-bin/draft/batchget?access_token=" + tok
    items = []
    total = None
    offset = 0
    for _ in range(max_pages):
        body = json.dumps({"offset": offset, "count": page_size, "no_content": 0}).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        data = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
        errcode = data.get("errcode", 0)
        if errcode not in (0, None):
            raise RuntimeError("draft/batchget 失败 [%s]: %s"
                               % (errcode, data.get("errmsg", "未知错误")))
        if total is None:
            total = int(data.get("total_count", 0) or 0)
        batch = data.get("item", []) or []
        items.extend(batch)
        offset += len(batch)
        if not batch or offset >= total:
            break
    total = total if total is not None else len(items)
    # 🔴 Codex JOS-027：达到 max_pages 上限但仍有未取部分时，
    #    旧实现仍返回 ok=True，调用方会以为「全库就这么多」。
    #    这里显式标记 truncated，并把 ok 置为 False，避免假成功。
    truncated = offset < total
    return {"ok": not truncated,
            "truncated": truncated,
            "total_count": total,
            "item_count": len(items),
            "pages_fetched": min(max_pages, (offset + page_size - 1) // page_size),
            "note": ("已达 max_pages=%d 上限，仍有 %d 篇未取回；"
                     "请提高 max_pages 或缩小 page_size 后重试"
                     % (max_pages, total - offset)) if truncated else "",
            "item": items}


def cn(t):
    return len(re.findall(r"[\u4e00-\u9fff]", strip_tags(t)))


def _asserts(html, title, digest, local, track_range=None, item=None):
    """独立断言集，返回 (errors, warns, info)。

    注意（2026-10-13 踩坑）：
        微信公众号存在两个不同字段，必须区分：
            thumb_media_id  封面素材 ID —— 有它才算「有封面」
            show_cover_pic  是否在正文顶部展示封面图（0/1），与「有没有封面」无关
        早期脚本把 show_cover_pic=0 误判为「封面丢失」，造成假警报。
    """
    errors, warns, info = [], [], {}
    plain = strip_tags(html)
    item = item or {}

    # 1. 封面（判据：item 中的 thumb_media_id 必须非空）
    #    🔴 2026-10-16 铁令：实测证明，若草稿丢失 thumb_media_id，
    #       微信后台草稿箱列表中将显示为空白图，读者与创作者均看不到封面！
    #       绝不能依赖 local 兜底或仅靠 thumb_url 判定，必须断言线上 item["thumb_media_id"] 真实存在。
    thumb_id = item.get("thumb_media_id", "")
    thumb_url = item.get("thumb_url", "")
    info["has_cover"] = bool(thumb_id)
    info["thumb_media_id"] = thumb_id
    info["thumb_url"] = thumb_url[:60] if thumb_url else ""
    info["show_cover_pic"] = item.get("show_cover_pic", None)
    if not thumb_id:
        errors.append("封面缺失：线上草稿 thumb_media_id 为空（草稿箱列表无法渲染封面卡片）")

    # 2. 标题逐字一致
    info["title_online"] = title
    info["title_local"] = local.get("title", "")
    if local.get("title") and title != local["title"]:
        errors.append("标题与本地不一致：线上「%s」 vs 本地「%s」"
                      % (title[:30], local["title"][:30]))

    # 3. 摘要
    if local.get("digest") and digest != local["digest"]:
        warns.append("摘要与本地不一致")

    # 3.5 正文逐字等价（JOS-010）
    #     历史实现只比 title/digest，导致「合成不同正文仍 verify 通过」。
    #     微信会把 HTML 重新序列化，故以「纯文本归一化后逐字比较」为准；
    #     本地未提供正文时明确记为 unknown，而不是默认通过。
    local_content = local.get("content") or ""
    if local_content:
        def _norm_text(value):
            text = strip_tags(value)
            text = text.replace("\u200b", "").replace("\ufeff", "")
            # 微信会注入零宽字符与不间断空格，统一后逐字比较
            text = text.replace("\xa0", " ")
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\s*\n\s*", "\n", text)
            return text.strip()

        online_text = _norm_text(html)
        local_text = _norm_text(local_content)
        if online_text != local_text:
            first_diff = next(
                (i for i, (a, b) in enumerate(zip(online_text, local_text)) if a != b),
                min(len(online_text), len(local_text)))
            errors.append(
                "正文与本地不一致：线上 %d 字 / 本地 %d 字，首个差异位于第 %d 字（线上「%s」 vs 本地「%s」）"
                % (len(online_text), len(local_text), first_diff,
                   online_text[first_diff:first_diff + 12],
                   local_text[first_diff:first_diff + 12]))
        else:
            info["body_equivalent"] = True
    else:
        warns.append("本地未提供正文，无法做逐字等价核验（body=unknown）")
        info["body_equivalent"] = None

    # 4. 转义乱码
    esc = [x for x in ("&lt;section", "&lt;blockquote", "&lt;h2", "style=&quot;")
           if x in html]
    if esc:
        errors.append("存在转义实体 %s" % esc)

    # 5. 编号重号
    dup = re.findall(
        r"<h2[^>]*>(?:\s*<[^>]+>)*\s*0?(\d{1,2})\s*[.、．]\s*0?\1\s*[.、．]",
        html, re.I)
    if dup:
        errors.append("章节重号 %s 处" % len(dup))

    # 6. 标签配平
    for tag in ("section", "blockquote", "p", "h2"):
        o = len(re.findall(r"<%s\b" % tag, html, re.I))
        c = len(re.findall(r"</%s>" % tag, html, re.I))
        if o != c:
            errors.append("<%s> 标签不平衡 %d/%d" % (tag, o, c))

    # 7. 图片全 CDN 与防静默丢失门禁（2026-10-17 铁令）
    imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html)
    bad_img = [u for u in imgs if "mmbiz.qpic.cn" not in u]
    info["img_count"] = len(imgs)
    if bad_img:
        errors.append("非微信 CDN 图片 %d 张" % len(bad_img))
    if len(imgs) == 0:
        errors.append("【图片静默丢失致命错误】线上草稿正文图片数为 0！微信已剔除非 CDN 图片，正文沦为纯文字！")

    # 8. 容量
    info["cn_count"] = cn(html)
    info["html_len"] = len(html)
    if track_range:
        lo, hi, hlo, hhi = track_range
        if not (lo <= info["cn_count"] <= hi):
            warns.append("汉字 %d 超出轨道区间 %d~%d"
                         % (info["cn_count"], lo, hi))
        if not (hlo <= info["html_len"] <= hhi):
            warns.append("HTML %d 超出轨道区间 %d~%d"
                         % (info["html_len"], hlo, hhi))

    # 9. 风控扫描
    lex = load_lexicon()
    r = scan(html, title, lex)
    info["risk_block"] = len(r["block"])
    info["risk_warn"] = (len(r["warn"]) + len(r["narrative"])
                         + len(r["literary"]) + len(r["identity"])
                         + len(r["fake_urgency"]))
    for item_r in r["block"]:
        errors.append("禁用词「%s」(于%s)" % (item_r["word"], item_r["scope"]))
    for item_r in r["identity"]:
        errors.append("身份越界「%s」" % item_r["word"])
    for item_r in r["fake_urgency"]:
        errors.append("伪时效「%s」" % item_r["word"])
    info["risk_detail"] = summarize(r)[2]

    # 10. 模板机械化口号禁令（2026-10-16 铁令）
    banned_slogans = [
        "当技术被写成神话",
        "我们去翻原始出处与一线账本",
        "在土地、水流与代码之间，为现代产业留一份清醒的观察坐标",
    ]
    found_slogans = [s for s in banned_slogans if s in html]
    if found_slogans:
        errors.append("包含已封禁文末机械化口号: %s" % found_slogans)

    return errors, warns, info


# ---------------------------------------------------------------- dump
def cmd_dump(mid):
    d = wechat_api.get_draft(mid)
    a = d["news_item"][0]
    out({"ok": True, "media_id": mid, "title": a.get("title", ""),
         "author": a.get("author", ""), "digest": a.get("digest", ""),
         "thumb_media_id": a.get("thumb_media_id", ""),
         "show_cover_pic": a.get("show_cover_pic", 1),
         "need_open_comment": a.get("need_open_comment", 0),
         "only_fans_can_comment": a.get("only_fans_can_comment", 0),
         "content_source_url": a.get("content_source_url", ""),
         "content": a.get("content", ""),
         "content_sha256": sha(a.get("content", "")),
         "cn_count": cn(a.get("content", ""))})


# ---------------------------------------------------------------- list
def cmd_list():
    r = fetch_all()
    items = []
    for it in r.get("item", []):
        a = it["content"]["news_item"][0]
        c = a.get("content", "")
        items.append({
            "media_id": it["media_id"],
            "title": a.get("title", ""),
            "title_sha256": sha(a.get("title", "")),
            "content_sha256": sha(c),
            "cn_count": cn(c),
            "html_len": len(c),
            "has_cover": bool(a.get("thumb_media_id")),
        })
    out({"ok": True, "total": r.get("total_count", 0), "items": items})


# ---------------------------------------------------------------- verify
def cmd_verify(mid, local_json_path=None):
    """独立回读断言。

    🔴 Codex 阻断项5：旧实现在缺 payload 时 `local={}` 静默通过，
    `body_equivalent=None`（未知）仍返回 ok=true、退出 0。
    「无法验证」不等于「验证通过」，这里改为未知即阻断。
    """
    local = {}
    payload_missing = not (local_json_path and os.path.exists(local_json_path))
    if not payload_missing:
        try:
            local = json.loads(Path(local_json_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            payload_missing = True
            local = {}
            print("⚠️ payload 不可读（%s），按未知处理" % exc)

    d = wechat_api.get_draft(mid)
    a = d["news_item"][0]
    html, title = a.get("content", ""), a.get("title", "")
    errors, warns, info = _asserts(html, title, a.get("digest", ""), local,
                                   item=a)

    unknown = []
    if payload_missing:
        unknown.append("缺少本地 payload，无法做正文/标题等价核验")
    if info.get("body_equivalent") is None:
        unknown.append("正文等价性未知（body_equivalent=None）")

    ok = (not errors) and (not unknown)
    out({"ok": ok, "media_id": mid, "errors": errors, "warns": warns,
         "unknown": unknown, "info": info}, 0 if ok else 1)


# ---------------------------------------------------------------- diff
def cmd_diff(mid=None, local=None):
    """比对线上指纹与本地记录。local 为 {content_sha256, title_sha256, title}。"""
    r = fetch_all()
    rows = []
    for it in r.get("item", []):
        a = it["content"]["news_item"][0]
        c = a.get("content", "")
        title = a.get("title", "")
        rec = {"media_id": it["media_id"], "title": title,
               "content_sha256": sha(c), "title_sha256": sha(title)}
        if mid and it["media_id"] != mid:
            continue
        rows.append(rec)
    if not local:
        out({"ok": True, "rows": rows})

    lh = local.get("content_sha256")
    lt = local.get("title_sha256")
    for rec in rows:
        if rec["content_sha256"] == lh and rec["title_sha256"] == lt:
            rec["status"] = "in_sync"
        elif rec["title_sha256"] != lt:
            rec["status"] = "diverged"
            rec["reason"] = "标题不一致（线上可能被手改）"
        else:
            rec["status"] = "online_drifted"
            rec["reason"] = "正文不一致（线上可能被手改）"
    out({"ok": True, "rows": rows})


# ---------------------------------------------------------------- push
def _idem_path(payload_dir, operation_id):
    """幂等状态文件路径：与 payload 同目录，按 operation_id 命名。"""
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', str(operation_id))[:120]
    return os.path.join(payload_dir, "receipt-%s.json" % safe)


def cmd_push(local_json_path, mode="update"):
    """执行推送。

    🔴 Codex B01：旧实现完全不读 operation_id / target_slug / payload.mode，
    导致同一操作重试会重复建稿，且回执不带操作标识。
    现在补三道：
      1) payload.mode 与调用参数 mode 必须一致（防止 update 被降级成 new）；
      2) operation_id 必填，作为幂等键；
      3) 已成功执行过的 operation_id 直接回放原回执，不再产生第二次建稿。
    """
    local = json.loads(Path(local_json_path).read_text(encoding="utf-8"))
    payload_dir = os.path.dirname(os.path.abspath(local_json_path))

    # ---- 1) 操作身份与目标绑定 ----
    operation_id = str(local.get("operation_id") or "").strip()
    if not operation_id:
        raise ValueError(
            "【操作标识缺失】payload 必须包含 operation_id；"
            "无标识的推送无法保证重试幂等，拒绝执行。")
    declared_mode = str(local.get("mode") or "").strip()
    if not declared_mode:
        raise ValueError(
            "【模式缺失】payload 必须显式声明 mode（new/update）；"
            "缺省会让『update 被静默降级为 new』，拒绝执行。")
    if declared_mode != str(mode):
        raise ValueError(
            "【模式不一致】payload 声明 mode=%r，调用方传入 mode=%r；"
            "拒绝以不一致的模式执行（防止 update 被降级为 new 而重复建稿）。"
            % (declared_mode, mode))
    target_slug = str(local.get("target_slug") or "").strip()
    if not target_slug:
        raise ValueError(
            "【目标缺失】payload 必须显式声明 target_slug；无目标绑定无法防止串稿。")
    article_dir = None
    if target_slug:
        # payload 通常位于 <稿件目录>/.studio/push_runs/<run_id>/payload.json，
        # 也可能位于稿件目录本身。向上找**含 content.json 的目录**即为稿件目录。
        probe = os.path.abspath(payload_dir)
        article_dir = None
        for _ in range(5):
            if os.path.isfile(os.path.join(probe, "content.json")):
                article_dir = probe
                break
            nxt = os.path.dirname(probe)
            if nxt == probe:
                break
            probe = nxt
        if article_dir:
            actual_slug = os.path.basename(article_dir)
            if actual_slug != target_slug:
                raise ValueError(
                    "【目标不符】payload 声明 target_slug=%r，实际所在稿件目录=%r"
                    % (target_slug, actual_slug))

    if article_dir is None:
        raise ValueError('目标绑定失败：找不到包含content.json的稿件目录')
    if mode not in ('new', 'update') or (mode == 'update' and not str(local.get('media_id') or '').strip()):
        raise ValueError('推送模式无效或update缺少media_id')
    # Bind semantic payload bytes (not its transport location) to the operation.
    payload_sha256 = __import__('hashlib').sha256(json.dumps(
        local, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

    # ---- 2) 幂等：同一 operation_id 只执行一次 ----
    # 🔴 Codex 第三轮 B01：旧实现有三个洞：
    #    a) 只在「成功后」写记录 → 提交成功但回读超时/落盘失败时，重试会再次 add；
    #    b) 检查与写入没有互斥 → 两个线程同时进来都能通过检查；
    #    c) 落盘失败被 try/except 吞掉 → 假成功。
    #    现在改为「写前登记（pending）+ 文件锁互斥 + 落盘失败即中止」。
    from scripts.job_lock import job_lock

    state_dir = os.path.join(article_dir, '.studio', 'operations')
    os.makedirs(state_dir, exist_ok=True)
    # Hash the full identity to avoid truncation/sanitisation collisions.
    idem_f = _idem_path(state_dir, __import__('hashlib').sha256(operation_id.encode()).hexdigest())
    lock_f = idem_f + ".lock"

    def _read_idem():
        if not os.path.isfile(idem_f):
            return None
        try:
            return json.loads(Path(idem_f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 记录损坏 = 状态未知，绝不能当作「未执行过」
            return {"status": "corrupt"}

    def _write_idem(rec):
        """写入状态；🔴 失败必须抛出，不能假成功。"""
        tmp = idem_f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, idem_f)

    def _replay(prior):
        out({"ok": True, "replayed": True,
             "operation_id": operation_id,
             "media_id": prior.get("media_id"),
             "title": prior.get("title", ""),
             "cn_count": prior.get("cn_count", 0),
             "thumb_media_id": prior.get("thumb_media_id", ""),
             "has_cover": bool(prior.get("thumb_media_id")),
             "content_sha256": prior.get("content_sha256", "")})

    try:
        with job_lock(lock_f):
            prior = _read_idem()
            if prior and (prior.get('operation_id') != operation_id or prior.get('payload_sha256') != payload_sha256):
                raise RuntimeError('操作身份冲突：相同operation_id绑定的目标或内容已变化')
            st = (prior or {}).get("status")

            if st == "succeeded":
                print("[idempotent] operation_id=%s 已成功执行过，回放原回执"
                      % operation_id, file=sys.stderr)
                return _replay(prior)

            if prior is not None and st != "failed":
                # 🔴 关键：提交结果未知时**禁止盲目再次 add**。
                #    平台侧无法查「上次是否已建稿」，自动重试会重复建稿。
                raise RuntimeError(
                    "【操作结果不确定】operation_id=%s 上次进入了 %s 状态"
                    "（可能已提交但未确认）。为避免重复建稿，拒绝自动重试；"
                    "请先人工核对草稿箱，确认后再用新的 operation_id 重推。"
                    % (operation_id, st))

            # A succeeded replay above is read-only; every actual write needs current review.
            from scripts.editorial_policy import submitted_review_errors
            writing_errors = submitted_review_errors(article_dir, local)
            if writing_errors:
                raise ValueError('正文编辑复核阻断：' + '; '.join(writing_errors))

            # ---- 写前登记 pending（在任何 API 调用之前）----
            _write_idem({"operation_id": operation_id, "payload_sha256": payload_sha256, "status": "pending",
                         "mode": mode, "target_slug": target_slug,
                         "media_id": local.get("media_id"),
                         "recorded_at": __import__("datetime").datetime.now(
                             __import__("datetime").timezone.utc).isoformat()})
    except (RuntimeError, ValueError):
        raise
    except Exception as exc:
        # 锁/登记失败 = 无法保证幂等，直接中止
        raise RuntimeError("【幂等登记失败】无法建立 operation_id=%s 的执行状态：%s"
                           % (operation_id, exc))

    def _record(status, media_id=None, extra=None):
        rec = {"operation_id": operation_id, "payload_sha256": payload_sha256, "status": status,
               "mode": mode, "target_slug": target_slug,
               "media_id": media_id,
               "recorded_at": __import__("datetime").datetime.now(
                   __import__("datetime").timezone.utc).isoformat()}
        if extra:
            rec.update(extra)
        with job_lock(lock_f):
            _write_idem(rec)

    # ---- 3) 正常执行 ----
    thumb_id = local.get("thumb_media_id", "")
    if not thumb_id:
        cover_path = os.path.join(article_dir, "cover_900x383.jpg")
        if os.path.exists(cover_path):
            thumb_id = wechat_api.upload_thumb_image(cover_path)
        else:
            raise FileNotFoundError("【封面缺失致命错误】缺少 thumb_media_id 且未找到封面文件: %s" % cover_path)
    if not thumb_id:
        raise ValueError("【封面缺失致命错误】thumb_media_id 为空，严禁推送无封面草稿！")

    art = {
        "title": local.get("title", ""),
        "author": local.get("author", "Jackie") or "Jackie",
        "digest": local.get("digest", ""),
        "content": local.get("content", ""),
        "thumb_media_id": thumb_id,
        "show_cover_pic": local.get("show_cover_pic", 1),
        "need_open_comment": local.get("need_open_comment", 0),
        "only_fans_can_comment": local.get("only_fans_can_comment", 0),
        "content_source_url": local.get("content_source_url", ""),
    }
    mid = local.get("media_id")
    # 🔴 Codex 第三轮：update 缺 media_id 时旧实现会 fallthrough 到 add_draft，
    #    操作类型被静默改变。这里严格区分：update 必须有 media_id，否则拒绝。
    if str(mode) == "update":
        if not str(mid or "").strip():
            _record("failed", extra={"reason": "update_without_media_id"})
            raise ValueError(
                "【update 缺 media_id】mode=update 必须提供 media_id；"
                "禁止静默降级为新建草稿（会产生重复稿）。")
        wechat_api.update_draft(mid, art)
    elif str(mode) == "new":
        mid = wechat_api.add_draft([art])
    else:
        _record("failed", extra={"reason": "unknown_mode"})
        raise ValueError("【未知模式】mode=%r 只接受 new/update" % (mode,))
    v = wechat_api.get_draft(mid)["news_item"][0]
    ret_thumb = v.get("thumb_media_id", "")
    if not ret_thumb:
        raise RuntimeError("【推送回读失败】草稿 %s 落库后 thumb_media_id 为空！" % mid)
    v_content = v.get("content", "")
    v_imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', v_content)
    sent_imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', art["content"])
    if sent_imgs and len(v_imgs) < len(sent_imgs):
        raise RuntimeError("【图片静默丢失致命错误】推送包含 %d 张图片，但微信草稿箱落库回读仅剩 %d 张！" % (len(sent_imgs), len(v_imgs)))
    # 🔴 Codex B01：回执必须带 operation_id，调用方才能校验归属。
    _rec = {"title": v.get("title", ""),
            "cn_count": cn(v.get("content", "")),
            "thumb_media_id": ret_thumb,
            "content_sha256": sha(v.get("content", ""))}
    _record("succeeded", media_id=mid, extra=_rec)
    out({"ok": True, "operation_id": operation_id,
         "media_id": mid, "title": v.get("title", ""),
         "cn_count": cn(v.get("content", "")),
         "thumb_media_id": ret_thumb,
         "has_cover": bool(ret_thumb),
         "content_sha256": sha(v.get("content", ""))})


# ---------------------------------------------------------------- audit
def cmd_audit():
    lex = load_lexicon()
    r = fetch_all()
    rows = []
    n_block = n_warn = 0
    for it in r.get("item", []):
        a = it["content"]["news_item"][0]
        html, title = a.get("content", ""), a.get("title", "")
        res = scan(html, title, lex)
        bc, wc, lines = summarize(res)
        # 额外结构断言
        struct = []
        if not a.get("thumb_media_id"):
            from scripts.cover_presence import verified_visible_cover
            if verified_visible_cover(_RUNTIME_ROOT, it['media_id'], a):
                wc += 1
                lines.append('封面真机可见；素材ID未回传，修改此稿前仍须确认非空素材ID')
            else:
                struct.append("封面绑定待核实：素材ID未回传且缺少有效真机证据")
        if len(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html)) == 0:
            struct.append("正文图片为0张(图片丢失)")
        if any(s in html for s in ["当技术被写成神话", "为现代产业留一份清醒的观察坐标"]):
            struct.append("含已封禁文末口号")
        if re.search(r"&lt;(section|blockquote|h2)", html):
            struct.append("转义乱码")
        if re.findall(r"<h2[^>]*>(?:\s*<[^>]+>)*\s*0?(\d{1,2})\s*[.、．]\s*0?\1", html):
            struct.append("章节重号")
        n_block += bc + len(struct)
        n_warn += wc
        rows.append({
            "media_id": it["media_id"], "title": title,
            "block": bc + len(struct), "warn": wc,
            "struct": struct, "lines": lines,
            "cn_count": cn(html), "has_cover": bool(a.get("thumb_media_id")),
        })
    out({"ok": n_block == 0, "total": len(rows), "block_total": n_block,
         "warn_total": n_warn, "rows": rows}, 0 if n_block == 0 else 1)


# ---------------------------------------------------------------- bidiff
def cmd_bidiff():
    """服务端只返回线上指纹；本地资产与台账比对在本地完成（JOS-035）。

    历史实现试图在服务器上加载 scripts/ops/bidirectional_diff.py 并调用
    find_local_dir(mid, m)（签名需 3 参）与 fetch_online（函数不存在），
    且该模块硬编码 Windows 根路径 —— 在 Linux 上必然崩溃。
    这里改为明确的单向职责：本函数不读本地目录，只输出官方指纹。
    """
    items = []
    for it in fetch_all().get("item", []):
        news = (it.get("content") or {}).get("news_item") or [{}]
        first = news[0]
        items.append({
            "media_id": it.get("media_id"),
            "title": first.get("title", ""),
            "content_sha256": sha(first.get("content", "")),
            "title_sha256": sha(first.get("title", "")),
            "thumb_media_id": first.get("thumb_media_id", ""),
            "update_time": it.get("update_time"),
        })
    out({"ok": True, "scope": "online_fingerprints_only",
         "note": "本地↔台账比对请在本机运行 scripts/ops/bidirectional_diff.py",
         "items": items})


# ---------------------------------------------------------------- listfull
def cmd_listfull():
    """列出全部草稿的**完整**指纹（逐篇 get_draft，非 batchget）。

    供本地做双向漂移比对使用。batchget 的 content 不可靠，
    因此这里对每篇单独调 get_draft。
    """
    r = fetch_all()
    items = []
    for it in r.get("item", []):
        mid = it["media_id"]
        try:
            a = wechat_api.get_draft(mid)["news_item"][0]
        except Exception as e:
            items.append({"media_id": mid, "error": str(e)[:60]})
            continue
        c = a.get("content", "")
        t = a.get("title", "")
        items.append({
            "media_id": mid,
            "title": t,
            "title_sha256": sha(t),
            "content_sha256": sha(c),
            "content": c,
            "cn_count": cn(c),
            "html_len": len(c),
            "thumb_media_id": a.get("thumb_media_id", ""),
            "thumb_url": a.get("thumb_url", ""),
            "has_cover": bool(a.get("thumb_media_id") or a.get("thumb_url")),
        })
    out({"ok": True, "total": len(items), "items": items})


def cmd_delete(media_id):
    """删除草稿。"""
    ok = wechat_api.delete_draft(media_id)
    out({"ok": ok, "media_id": media_id})


def main():
    if len(sys.argv) < 2:
        out({"ok": False, "error": "usage: mp_ops.py <cmd> ..."}, 2)
    cmd = sys.argv[1]
    args = sys.argv[2:]
    try:
        if cmd == "dump":
            cmd_dump(args[0])
        elif cmd == "delete":
            cmd_delete(args[0])
        elif cmd == "list":
            cmd_list()
        elif cmd == "verify":
            lp = args[1] if len(args) > 1 else None
            cmd_verify(args[0], lp)
        elif cmd == "diff":
            lp = args[1] if len(args) > 1 else None
            local = json.loads(Path(lp).read_text(encoding="utf-8")) if lp and os.path.exists(lp) else None
            cmd_diff(args[0] if args and args[0] != "-" else None, local)
        elif cmd == "push":
            cmd_push(args[0], args[1] if len(args) > 1 else "update")
        elif cmd == "audit":
            cmd_audit()
        elif cmd == "bidiff":
            cmd_bidiff()
        elif cmd == "listfull":
            cmd_listfull()
        else:
            out({"ok": False, "error": "unknown cmd %s" % cmd}, 2)
    except Exception as e:
        out({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 1)


if __name__ == "__main__":
    main()
