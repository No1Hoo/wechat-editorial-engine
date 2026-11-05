#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jackie 观察视角 · 统一生产与发布调度中枢 (Jackie Unified Pipeline)
终结脚本碎片化与模型排版漂移，实现四大轨道 (A/B/C/D) 全流程标准化闭环。

子命令支持:
  render       根据目录下的 content.json 与静态模板编译 article.html 和 preview.html
  check        执行轨道刚性前置质检门禁 (Pre-Flight Gate, 含 Gate 1-16)
  deploy       一键直连腾讯云同步 CDN 并入库微信公众平台草稿箱
  edit-images  按需轻量唤醒配图工作台微调裁切 (非必经步骤)

  ── 批次 1 新增（交付层防漂移 · 2026-10-13）──
  diff         比对本地与线上指纹，检测前台手改造成的漂移
  push         本地覆盖线上（强制先跑 check + diff）
  verify       部署后独立回读断言（封面/标题/乱码/容量/编号/图片）
  audit        全库风控体检（禁用词/伪时效/身份越界/叙事层/文学化）
  release      check → diff → push → verify → audit 一条命令

策略声明:
  **本地为准、线上服从本地**。线上被前台手改时，diff 会阻断 push 并要求人工裁决。
"""

import os
import sys
import json
import re
import shlex
import argparse
import subprocess
import hashlib
import time
from pathlib import Path

# 设置标准编码
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

ROOT_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT_DIR / "templates"

try:
    import jinja2
except ImportError:
    jinja2 = None

def count_chinese(text):
    """计算纯汉字字数"""
    clean = re.sub(r'<[^>]+>', '', text)
    return len(re.findall(r'[\u4e00-\u9fff]', clean))

# -------------------------------------------------------------
# 1. 核心渲染器 (Render)
# -------------------------------------------------------------
def cmd_render(args):
    target_dir = Path(args.dir).resolve()
    track = args.track.upper()
    print(f"\n[*] 启动流水线渲染: 目标目录={target_dir}, 轨道={track}")

    # 🔴 Codex 阻断项6：废弃轨道（B/C/E/S）此前仍能通过 CLI 渲染，
    #    等于「注销」只是纸面声明。生产入口必须硬拒绝。
    try:
        sys.path.insert(0, str(ROOT_DIR / "scripts"))
        from track_policy import require_active
        require_active(track)
    except ImportError:
        if track not in {"A", "D", "F"}:
            raise ValueError("轨道 %s 已注销，禁止渲染" % track)

    if jinja2 is None:
        raise RuntimeError("缺失 jinja2 模块，请先运行: pip install jinja2")

    content_file = target_dir / "content.json"
    if not content_file.exists():
        raise FileNotFoundError(f"未找到数据文件: {content_file}，请确保 content.json 已就绪。")

    content_data = json.loads(content_file.read_text(encoding="utf-8"))

    # 加载 CDN 映射（若已有）或 content.json 中的配图映射
    cdn_cache = target_dir / "cdn_urls.json"
    img_urls = dict(content_data.get("img_urls", {}))
    if cdn_cache.exists():
        try:
            cdn_map = json.loads(cdn_cache.read_text(encoding="utf-8"))
            img_urls.update(cdn_map)
        except Exception:
            pass

    # 历史默认图键（p1~p5 / c1~c3）曾无条件注入，导致新稿缺图时静默引用旧稿图片（JOS-038）。
    # 现改为「存在性门控」：只有当该稿目录下确实存在对应文件时才映射，否则保持缺失，
    # 由 Gate 17 如实报错。这样既有 11 篇合法旧稿不被破坏，也不会跨稿兜底。
    legacy_defaults = {
        "p1": "photos/photo1_farming.jpg",
        "p2": "photos/photo2_vessel.jpg",
        "p3": "photos/photo3_underwater.jpg",
        "p4": "photos/photo4_deepsea.jpg",
        "p5": "photos/photo5_harvest_drying.jpg",
        "c1": "card1_carbon_leakage_model.png",
        "c2": "card2_cost_waterfall_inversion.png",
        "c3": "card3_blue_carbon_regulatory_storm.png",
    }
    for k, v in legacy_defaults.items():
        if k in img_urls:
            continue
        if (target_dir / v).is_file():
            img_urls[k] = v

    # 🔴 Codex JOS-034：CLI 此前自建一套非严格的 jinja Environment，
    #    与 Studio 的 content_contract.render_article（StrictUndefined + autoescape）
    #    产生**不同字节**的产物（实测 535 vs 534）。同一份 content.json 在不同入口
    #    得到不同 HTML，是所有「字节指纹 / manifest / 回读比对」失真的根源。
    #    这里改为共用唯一入口，只保留 CLI 特有的「编号去重」预处理。
    try:
        from scripts.content_contract import render_article as _render_article
        _has_contract = True
    except Exception:
        _has_contract = False

    render_context = dict(content_data)
    # 注意：content_contract.render_article 会自己把 img_urls 作为独立参数传给模板，
    # 因此 content 里不能再带同名键，否则 render 会收到重复关键字参数。
    render_context.pop("img_urls", None)

    # 🔴 章节编号去重（2026-10-13 根治）：
    # 模板会自动加 "0N. " 前缀，但 content.json 里若已手写编号（如 "05. 一线算账…"），
    # 渲染结果会变成 "05. 05. 一线算账…"。此处统一剥离手写前缀，杜绝重号。
    _dst = render_context.get("decision_section_title")
    if isinstance(_dst, str) and _dst.strip():
        render_context["decision_section_title"] = re.sub(
            r'^\s*0?\d{1,2}\s*[.、．]\s*', '', _dst).strip()

    if _has_contract:
        # 注意：render_article 返回的字符串带模板前导换行，
        # 这里与 Studio 保持一致——**不做 strip**，否则两入口字节再次分叉。
        article_body = _render_article(
            render_context, track,
            image_urls=img_urls,
            industry=(render_context.get("industry")
                      or render_context.get("track")
                      or getattr(args, "track_type", None)))
    else:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
            undefined=jinja2.StrictUndefined, autoescape=True)
        template_name = f"track_{track.lower()}.html.j2"
        template = env.get_template(template_name)
        article_body = template.render(**render_context).strip()

    # 🔴 覆盖保护（2026-10-13 最后一漏洞修复）：
    # 背景：content.json 的 sections[].paragraphs 只是**写作草稿**，
    #       而 article.html 可能是**线上渲染态**（含图注/卡片/callout/引语块），
    #       实测 19 篇的 article.html 汉字数远多于 content.json 段落实数。
    #       若无条件写入，一次 render 就会把线上态覆盖成残稿。
    # 策略：新渲染内容明显短于现有 article.html 时拒绝写入，需 --force 才覆盖。
    article_html_path = target_dir / "article.html"
    if article_html_path.exists():
        old_html = article_html_path.read_text(encoding="utf-8")
        old_cn = count_chinese(old_html)
        new_cn = count_chinese(article_body)
        force = getattr(args, "force", False)
        # 现有文件是否为「线上渲染态」（含微信注入的 leaf 标签）？
        online_state = ('leaf=""' in old_html or 'data-src=' in old_html)
        # 线上态转模板态会去掉 leaf 标签（体积变小但内容不减），
        # 因此对线上态放宽阈值到 95%，重点拦截**真实内容丢失**。
        threshold = 0.95 if online_state else 0.80
        if old_cn > 0 and new_cn < old_cn * threshold and not force:
            print("\n" + "!" * 62)
            print("⛔ 覆盖保护已触发：拒绝写入 article.html")
            print("   现有文件汉字数: %d%s" % (old_cn, "（线上渲染态）" if online_state else ""))
            print("   新渲染汉字数  : %d（低于现有的 %.0f%%）" % (new_cn, threshold * 100))
            print("   原因：content.json 与 article.html 可能不同源，直接覆盖会丢内容。")
            print("   若确认要覆盖：加 --force")
            print("   若想保留现有：跳过本步骤（article.html 未改动）")
            print("!" * 62)
            raise SystemExit(3)
        if old_cn > 0:
            delta = new_cn - old_cn
            print("   ℹ️ 汉字数对比：现有 %d → 新渲染 %d（%+d）%s"
                  % (old_cn, new_cn, delta,
                     "（线上态→模板态，属正常）" if online_state else ""))

    # 写入 article.html（换行统一为 \n，保证跨平台字节一致）
    article_html_path.write_bytes(article_body.encode("utf-8"))

    # 渲染浏览器预览 preview.html
    # 预览模板与正文模板用同一套严格环境（JOS-034：不允许两套 jinja 配置并存）。
    preview_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=jinja2.StrictUndefined, autoescape=True)
    preview_template = preview_env.get_template("preview.html.j2")
    preview_full = preview_template.render(
        title=content_data.get("title", "文章预览"),
        article_body=article_body
    )
    preview_path = target_dir / "preview.html"
    preview_path.write_bytes(preview_full.encode("utf-8"))

    # 生成构建清单：把模板/政策/内容/资产绑定为可核验指纹（JOS-034）。
    # 与 delivery_gate / receipt_store 读取的字段严格一致。
    _write_build_manifest(target_dir, track, content_data, article_body,
                          industry=(render_context.get('industry') or render_context.get('track')
                                    or getattr(args, 'track_type', None)))

    pure_cn = count_chinese(article_body)
    html_len = len(article_body)
    print(f"🎉 渲染成功！")
    print(f"   - 纯正文汉字: {pure_cn} 字")
    print(f"   - HTML 字符数: {html_len} 字符")
    print(f"   - 输出文件: {article_html_path}")
    print(f"   - 预览文件: {preview_path}")


def _referenced_local_assets(html_text, target_dir):
    from scripts.asset_contract import local_assets
    return local_assets(html_text, target_dir)


def _assets_fingerprint(rel_paths, target_dir):
    from scripts.asset_contract import fingerprint
    return fingerprint(rel_paths, target_dir)


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _read_title(target_dir):
    """读取稿件标题（用于「无变化不写」判定，Codex 阻断项4b）。"""
    try:
        data = json.loads((Path(target_dir) / "content.json").read_text(encoding="utf-8"))
        return str(data.get("title", ""))
    except (OSError, ValueError):
        return ""


def _write_build_manifest(target_dir, track, content_data, article_body, industry=None):
    """写 build_manifest.json；字段与 delivery_gate 的 expected 一一对应。"""
    import datetime as _dt

    def _file_sha(path):
        p = Path(path)
        return _sha256_bytes(p.read_bytes()) if p.is_file() else None

    content_path = target_dir / "content.json"
    html_bytes = article_body.encode("utf-8")
    assets = []
    for name in ("cover_900x383.jpg",):
        p = target_dir / name
        if p.is_file():
            assets.append({"path": name, "sha256": _file_sha(p)})
    # 正文实际引用的本地图片也纳入清单
    for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', article_body):
        src = m.group(1)
        if src.startswith(("http://", "https://")):
            continue
        rel = src.replace("\\", "/").lstrip("./")
        p = target_dir / rel
        if p.is_file() and not any(a["path"] == rel for a in assets):
            assets.append({"path": rel, "sha256": _file_sha(p)})
    # 开篇导语图纳入清单
    if isinstance(content_data.get('lead_image'), dict) and content_data['lead_image'].get('key'):
        rel = str(content_data['lead_image']['key']).replace('\\', '/').lstrip('./')
        p_lead = target_dir / rel
        if p_lead.is_file() and not any(a['path'] == rel for a in assets):
            assets.append({'path': rel, 'sha256': _file_sha(p_lead)})
    # 同时确保 content_data 引用的所有本地源配图纳入清单（满足 delivery_gate 要求）
    for sec in content_data.get('sections', []):
        for k in ('image', 'extra_image', 'final_card', 'gif_or_image'):
            item = sec.get(k)
            if isinstance(item, dict) and item.get('key'):
                rel = str(item['key']).replace("\\", "/").lstrip("./")
                p_item = target_dir / rel
                if p_item.is_file() and not any(a["path"] == rel for a in assets):
                    assets.append({"path": rel, "sha256": _file_sha(p_item)})
        for img in sec.get('images', []):
            if isinstance(img, dict) and img.get('key'):
                rel = str(img['key']).replace("\\", "/").lstrip("./")
                p_img = target_dir / rel
                if p_img.is_file() and not any(a["path"] == rel for a in assets):
                    assets.append({"path": rel, "sha256": _file_sha(p_img)})

    from scripts.content_contract import template_fingerprint
    build_industry = industry or content_data.get('industry') or content_data.get('track') or 'agri-tech'
    template_identity = template_fingerprint(content_data, track, build_industry)

    # 若本地配图已被原位替换（SHA256 相比旧 build_manifest 改变），自动失效 cdn_urls.json 中对应的旧 CDN URL
    old_manifest_path = target_dir / 'build_manifest.json'
    cdn_cache_path = target_dir / 'cdn_urls.json'
    if old_manifest_path.is_file() and cdn_cache_path.is_file():
        try:
            old_m = json.loads(old_manifest_path.read_text(encoding='utf-8'))
            old_shas = {a['path']: a.get('sha256') for a in old_m.get('assets', []) if isinstance(a, dict) and a.get('path')}
            cdn_cache = json.loads(cdn_cache_path.read_text(encoding='utf-8'))
            if isinstance(cdn_cache, dict):
                purged = []
                for a in assets:
                    rel_p = a['path']
                    if rel_p in old_shas and old_shas[rel_p] != a['sha256'] and rel_p in cdn_cache:
                        cdn_cache.pop(rel_p, None)
                        purged.append(rel_p)
                if purged:
                    cdn_cache_path.write_text(json.dumps(cdn_cache, ensure_ascii=False, indent=2), encoding='utf-8')
                    print(f'   ♻️ 检测到配图内容更新，已自动失效旧 CDN 缓存: {purged}')
        except Exception:
            pass

    manifest = {
        "manifest_schema": 2,
        "content_track": track,
        "industry": build_industry,
        "content_sha256": _file_sha(content_path),
        # V302：按**实际使用**的模板算指纹，不再硬编码主模板。
        # 旧实现下，presentation=A 的稿件与 presentation=B 的稿件
        # template_sha256 完全相同，改实际模板也发现不了。
        **template_identity,
        "renderer_sha256": _file_sha(ROOT_DIR / "scripts" / "content_contract.py"),
        "compliance_rules_sha256": _file_sha(ROOT_DIR / "scripts" / "preflight_compliance.py"),
        "topic_spec_sha256": _file_sha(target_dir / "topic_spec.json"),
        "editorial_policy_sha256": _file_sha(ROOT_DIR / "references" / "editorial_policy.json"),
        "writing_gate_sha256": _file_sha(ROOT_DIR / "scripts" / "editorial_policy.py"),
        "html_sha256": _sha256_bytes(html_bytes),
        "assets": assets,
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    payload = {k: v for k, v in manifest.items() if k not in ("build_hash", "created_at")}
    manifest["build_hash"] = _sha256_bytes(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    (target_dir / "build_manifest.json").write_bytes(
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))

# -------------------------------------------------------------
# 2. 刚性前置质检门禁 (Check / Pre-flight)
# -------------------------------------------------------------
def named_entity_warnings(content, html_text):
    """Gate 19（R11）：具名引语主体须在 content.json 的 named_entities 登记。

    只对「主体 + 引语动词」模式告警，避免把普通名词误判成人名。
    首周为 warn 级，观察误报率后升 block。
    """
    if not isinstance(content, dict):
        return []
    named = content.get("named_entities") or []
    if not isinstance(named, list):
        return ["【Gate 19】named_entities 须为列表"]
    declared = {str(e.get("name", "")).strip() for e in named
                if isinstance(e, dict) and e.get("name")}
    body = re.sub(r"<[^>]+>", "", html_text or "")
    body = body.replace("\u200b", "").replace("\xa0", " ")
    generic = {"记者", "笔者", "专家", "教授", "研究员", "农户", "养殖户",
               "渔民", "工人", "老板", "负责人", "创始人", "董事长", "总经理",
               "村民", "消费者", "买家", "卖家", "员工", "公司", "企业", "机构",
               "平台", "官方", "政府", "法院", "部门", "协会", "数据", "报告",
               "业内", "媒体", "网友", "客户", "供应商", "经销商", "股东"}
    quoted = set(re.findall(
        r"([\u4e00-\u9fff]{2,4})(?:告诉|对|向)?(?:记者|本刊)?(?:说|表示|坦言|承认|回忆|介绍|透露)",
        body))
    unknown = sorted(n for n in quoted if n not in declared and n not in generic)
    if not unknown:
        return []
    return ["【Gate 19 具名主体待登记】正文出现引语主体 %s，"
            "须在 content.json 的 named_entities 登记来源与证据快照"
            % "、".join(unknown[:6])]


def title_mode_errors(content, folder=None):
    """Gate 20 checks title evidence and comparable units, never a traffic promise."""
    from scripts.editorial_claims import title_mode_errors as check_title
    return check_title(content, folder)


# Gate 21：配图图注时间脱敏。判定落在 scripts/editorial_claims.caption_time_errors，
# 只查「展示给读者的图注」，不查正文（正文允许出现报道年份等来源说明）。
def caption_time_errors(content, folder=None):
    """Gate 21: image captions must not carry a concrete year/month."""
    from scripts.editorial_claims import caption_time_errors as check_caption
    return check_caption(content)


def cmd_check(args):
    target_dir = Path(args.dir).resolve()
    track = args.track.upper()
    track_type = args.track_type.lower()
    print(f"\n==========================================")
    print(f"🚀 开始执行【轨道 {track}】刚性前置质检门禁")
    print(f"📂 目标目录: {target_dir}")
    print(f"🎯 产业赛道: {track_type}")
    print(f"==========================================\n")

    errors = []
    warns = []
    source_path = target_dir / 'content.json'
    if source_path.exists():
        try:
            from scripts.editorial_policy import scan as writing_scan
            writing = writing_scan(json.loads(source_path.read_text(encoding='utf-8')))
            errors.extend(writing['errors'])
            warns.extend(writing['warnings'])
        except (OSError, ValueError, TypeError) as exc:
            errors.append('写作政策检查失败：' + str(exc))
    html_file = target_dir / "article.html"
    cover_file = target_dir / "cover_900x383.jpg"

    if not html_file.exists():
        errors.append(f"缺失关键 HTML 文件: {html_file}")
    if not cover_file.exists():
        errors.append(f"【Gate 1 黄金封面缺失】缺失 900x383 黄金裁切封面: {cover_file}")
    elif cover_file.stat().st_size < 10000:
        errors.append(f"【Gate 1 封面体积异常】900x383 封面体积过小 ({cover_file.stat().st_size} bytes < 10KB)，疑似损坏: {cover_file}")
    else:
        print(f"    ✅ Gate 1 黄金封面存在且有效 ({cover_file.stat().st_size} bytes)")

    if html_file.exists():
        html_content = html_file.read_text(encoding="utf-8")
        pure_cn = count_chinese(html_content)
        html_len = len(html_content)

        # 🔴 线上态识别（2026-10-13 批次 0 基线回收后新增）：
        # article.html 可能是「线上渲染态」（含微信注入的 <span leaf="">），
        # 与本地模板渲染态存在系统性差异（HTML 更长、根 section 被重写）。
        # 🔴 安全约束（JOS-011）：线上态只允许把「样式类」门禁降级为警告，
        #    绝不允许用正文里的一个字符串就豁免长度/封面/配图等生产准入。
        #    判定依据改为渲染器写入的结构化标记，而不是内容自述的 data-src。
        is_online_state = bool(re.search(
            r'<meta\s+name="jackie-render-state"\s+content="wechat_online"', html_content))
        legacy_online_hint = ('leaf=""' in html_content or 'data-src=' in html_content)
        if is_online_state:
            print("    ℹ️ 检测到渲染器标记的「线上态」article.html")
            print("       → 仅样式类门禁降级为警告；长度与资产门禁仍然生效")
        elif legacy_online_hint:
            print("    ⚠️ 正文含 leaf/data-src 迹象但无渲染器标记：按本地态严格校验")

        # 字数标准范围：唯一事实源 references/track_registry.json（JOS-012）
        # 说明：check 属于只读巡检，允许对历史废轨稿件执行；新建/渲染/推送才硬拒绝。
        try:
            sys.path.insert(0, str(ROOT_DIR / "scripts"))
            from track_policy import (
                ranges as _policy_ranges, deprecated_ranges as _legacy_ranges,
                deprecated_tracks as _deprecated_tracks)
            ranges = _policy_ranges()
            if track in _deprecated_tracks():
                warns.append(f"轨道 {track} 已注销：本次仅做历史只读校验，禁止据此新增生产")
                ranges = {**_legacy_ranges(), **ranges}
            elif track not in ranges:
                errors.append(f"未知轨道 {track}：不在 references/track_registry.json 中")
        except ImportError:
            ranges = {
                "A": (2200, 2800, 6500, 10500),
                "D": (2200, 2800, 6500, 10500),
                "F": (2200, 2800, 6500, 10500),
            }
        if track in ranges:
            min_cn, max_cn, min_h, max_h = ranges[track]
            print(f"[*] 纯正文汉字: {pure_cn} 字 (标准区间: {min_cn} ~ {max_cn})")
            if pure_cn < min_cn:
                errors.append(f"纯正文汉字不足 ({pure_cn} < {min_cn})")
            elif pure_cn > max_cn:
                errors.append(f"纯正文汉字超长 ({pure_cn} > {max_cn})")
            else:
                print("    ✅ 纯正文汉字数合规！")

            print(f"[*] HTML 字符数: {html_len} (标准区间: {min_h} ~ {max_h})")
            if html_len < min_h:
                errors.append(f"HTML 字符数过少 ({html_len} < {min_h})")
            elif html_len > max_h:
                errors.append(f"HTML 字符数超标 ({html_len} > {max_h})，存在拒收风险！")
            else:
                print("    ✅ HTML 字符数合规！")

        # 承接卡同赛道纯度（若存在承接卡则严查同赛道；若已按军规移除承接卡则通过）
        if "深度长文精选" in html_content or "Jackie 观察视角 ·" in html_content:
            if track_type == "fishery":
                forbidden = ["生菜", "草莓", "大田", "番茄", "温室", "楼房养猪", "育肥舍"]
                start_idx = max(html_content.find("深度长文精选"), html_content.find("Jackie 观察视角 ·"))
                rec_area = html_content[start_idx:start_idx+1500]
                found_bad = [w for w in forbidden if w in rec_area]
                if found_bad:
                    errors.append(f"承接卡出现跨物理赛道污染词汇: {found_bad}，水产赛道严禁推泥土或猪舍！")
                else:
                    print("    ✅ 承接卡赛道纯度 100% 水产同赛道匹对！")
        else:
            print("    ✅ 纯净出版物排版模式：零承接卡与营销号导流，以权威档案纯净收尾！")

        # 伪时效性与过时信息合规检查 (Anti-Fake Urgency Gate · 2026-10-07 杰哥令)
        # 严查“围绕旧闻翻炒、使用模糊时效词汇造成误导读者以为是近期突发”
        fake_urgency_words = ["就在最近", "刚刚爆雷", "突发爆雷", "突发猝死", "昨晚在X上炸锅"]
        found_urgency = [w for w in fake_urgency_words if w in html_content]
        if found_urgency:
            errors.append(f"触发微信‘过时信息’高危词汇拦截: {found_urgency}！历史案例严禁使用伪突发词汇，必须显式标明发生纪年并定性为商业复盘！")
        else:
            print("    ✅ 伪时效词汇审查通过，零高危诱导时效词！")

        # 4. 微信平台风控与标题违规词拦截 (Anti-Clickbait & Risk Control Gate)
        content_json_path = target_dir / "content.json"
        title_str = ""
        if content_json_path.exists():
            try:
                cdata = json.loads(content_json_path.read_text(encoding="utf-8"))
                title_str = cdata.get("title", "")
            except Exception:
                pass
        
        risk_words = ["废铁", "全池翻白", "干到", "爆雷", "猝死", "暗网", "黑客", "割韭菜", "智商税"]
        found_title_risk = [w for w in risk_words if w in title_str]
        if found_title_risk:
            errors.append(f"【标题风控违规拦截】标题中检测到高危负向煽动/触发黄色警示词汇: {found_title_risk}！必须换用理性、工业化、硬核算账式中性标题！")
        else:
            print("    ✅ 标题风控脱敏审查通过，零平台高危违规词！")

        if track == "S":
            clean_title = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', title_str)
            if len(clean_title) > 10:
                errors.append(f"【轨道 S 标题长度超标】轨道 S 标题必须严格 ≤ 10 个字，当前为 {len(clean_title)} 字（{title_str}）！")
            else:
                print(f"    ✅ 轨道 S 标题字数合规（{len(clean_title)} 字 ≤ 10 字）！")

        # 5. HTML 实体转义乱码终极绝育门禁 (Anti-Garbled HTML Entity Gate)
        # 严防微信前台 dump 数据未经 unescape 直接作为 HTML 提交导致全屏代码大乱码
        escaped_tags = ['&lt;section', '&lt;blockquote', '&lt;h2', '&lt;p', '&lt;div', 'style=&quot;', '&lt;/']
        found_escaped = [tag for tag in escaped_tags if tag in html_content]
        if found_escaped:
            errors.append(
                f"【HTML实体乱码致命红线】检测到未解码的 HTML 实体转义代码: {found_escaped}！"
                f"此内容入库微信公众平台后将被渲染为字面源码乱码！必须在入库前执行 html.unescape() 彻底还原原生标签！"
            )
        else:
            print("    ✅ HTML 实体防乱码门禁通过，100% 原生纯净富文本！")

        # 6. HTML 结构闭合与标签平衡门禁 (Tag Balancing Gate)
        open_sections = len(re.findall(r'<section\b', html_content, re.IGNORECASE))
        close_sections = len(re.findall(r'</section>', html_content, re.IGNORECASE))
        if open_sections != close_sections:
            errors.append(f"【HTML结构破损】<section> 标签不平衡: 打开 {open_sections} 个 vs 闭合 {close_sections} 个！")
        else:
            print(f"    ✅ HTML 结构配平闭合通过 ({open_sections} 对 section 完美对齐)！")

        # 7. 参考文献与资料来源真实性硬门禁 (Truth-Check & Fake Reference Hard Gate · 2026-10-08 根治铁令)
        # 仅轨道 A (深度调查母舰) 强制执行文献核验；B/C/D/S/E 轨道全面去学术化、轻量化，免除文末参考文献要求。
        if track == "A" and content_json_path.exists():
            cdata = json.loads(content_json_path.read_text(encoding="utf-8"))
            refs = cdata.get("references", [])
            for r_idx, ref in enumerate(refs):
                bad_bookmarks = re.findall(r'《([^》]+)》', ref)
                for bm in bad_bookmarks:
                    is_legal = False
                    if "年鉴" in bm:
                        is_legal = True
                    elif any(std in bm or std in ref for std in ["GB", "DB", "ISO", "HJ", "SC", "NY"]):
                        is_legal = True
                    elif "The State of World Fisheries and Aquaculture" in bm or "SOFIA" in ref:
                        is_legal = True
                    elif any(kw in bm for kw in ["学报", "现代化", "研究"]):
                        is_legal = True
                    
                    if not is_legal and any(fake_kw in bm for fake_kw in ["报告", "白皮书", "通报", "卷宗", "指导意见", "方案", "规划"]):
                        errors.append(
                            f"【假文献红线拦截】资料来源第 {r_idx+1} 条出现未经国家正式出版核验的书名号《{bm}》！"
                            f"军规铁律：严禁为媒体采写、行业会议PPT、司法执行或零散通报添加专有书名号，必须启动‘诚实降级规范’去除《》，如实标为事实陈述与检索词！"
                        )
                
                if "2026" in ref and any(kw in ref for kw in ["报告", "白皮书", "年鉴", "通报"]):
                    if any(fake_kw in ref for fake_kw in ["《2026", "2026年卷", "2026年度"]):
                        errors.append(f"【假文献红线拦截】资料来源第 {r_idx+1} 条涉嫌伪造未来年份出版物（{ref}），尚未出版的年份严禁加专有出版书名号！")
            
            if not any("假文献红线拦截" in e for e in errors):
                print("    ✅ 资料来源真实性审查通过，零伪造书名号与虚构专有报告！")
        elif track != "A":
            print(f"    ✅ 轨道 {track} 轻量化放行：非 A 轨全面免除参考文献与资料来源硬门禁！")

        # 8. 版式排版与对齐刚性门禁 (Typography & Strict Alignment Gate · 2026-10-09 铁令)
        # 8.1 标题左对齐与竖标门禁：每个 h2 必须带 border-left、padding-left 与 text-align:left
        #     线上态只降级为警告，但绝不跳过检查（JOS-011/JOS-013）。
        def _style_of(tag):
            m = re.search(r'style\s*=\s*"([^"]*)"', tag, re.I) or \
                re.search(r"style\s*=\s*'([^']*)'", tag, re.I)
            return (m.group(1) if m else '').replace(' ', '').lower()

        def _css_value(style, prop):
            """取 CSS 属性的**最终生效值**（Codex JOS-013）。

            旧实现用 `'text-align:left' in st` 做子串判断，
            于是 `text-align:left;text-align:center` 这种「先 left 后 center」
            的写法会被判为合规——但浏览器实际生效的是后写的 center。
            这里按声明顺序取最后一个值，与 CSS 层叠语义一致。
            """
            values = re.findall(
                r'(?:^|;)%s\s*:\s*([^;!]+)' % re.escape(prop), style)
            if not values:
                return None
            return values[-1].strip().lower()

        h2_tags = re.findall(r'<h2\b[^>]*>', html_content, re.IGNORECASE)

        def _px_nonzero(style, prop):
            """取属性值并判断是否为**非零**长度（Codex JOS-013）。

            旧正则 `[1-9]\\d*px` 会把合法的 `0.5px` 判为不合格——
            小数边框是真实存在的细线写法。这里改为数值解析：
            只要解析出的数值 > 0 即算非零，不限制整数。
            """
            val = _css_value(style, prop)
            if val is None:
                return False
            m = re.match(r'^(-?\d+(?:\.\d+)?)\s*(px|em|rem)?', val.strip())
            if not m:
                return False
            try:
                return float(m.group(1)) > 0
            except ValueError:
                return False

        def _bad_h2(tag):
            st = _style_of(tag)
            # 必须是有效的非零左侧边框 + 缩进 + 绝对左对齐
            if not _px_nonzero(st, 'border-left') and not _px_nonzero(st, 'border-left-width'):
                return True
            if not _px_nonzero(st, 'padding-left'):
                return True
            return _css_value(st, 'text-align') != 'left'

        # 🔴 Codex 阻断项6：线上态不能再把「样式不合格」降级为警告。
        #    伪 meta 只要能写进正文就能豁免门禁——这是内容自证。
        #    改为：线上态与本地态**同样阻断**，只是错误文案标注来源差异。
        bad_h2 = [tag for tag in h2_tags if _bad_h2(tag)]
        if bad_h2:
            state_note = "（线上态）" if is_online_state else ""
            errors.append(
                f"【标题排版对齐缺陷】{state_note}检测到未规范样式的 <h2> 标题: "
                f"{bad_h2[:3]}！必须显式包含非零 border-left、padding-left 以及 "
                f"text-align:left，严禁标题悬空与对齐漂移！")
        else:
            print("    ✅ 标题排版规范审查通过，全量 h2 具备左侧锚定色标与绝对左对齐！")

        # 8.2 语义化列表与悬挂缩进门禁：严禁用裸 '•' / '1.' 冒充原生列表
        pseudo = re.compile(
            r'<(p|div|section|span)\b[^>]*>\s*(?:•|·|▪|●|○|\d{1,2}\s*[.、．])\s*'
            r'(?:<strong>|<b>|<a\b)', re.I)
        naked_bullets = [m.group(0)[:80] for m in pseudo.finditer(html_content)]
        if naked_bullets:
            errors.append(f"【悬挂缩进排版缺陷】检测到裸符号伪列表 {len(naked_bullets)} 处: {naked_bullets[:3]}！卡片指标必须使用标准 <ul><li> 结构，杜绝移动端换行时第二行错位！")
        else:
            print("    ✅ 卡片指标悬挂缩进审查通过，全量列表符合语义化对齐！")

        # 8.3 正文两端对齐门禁：根 section 必须包含 text-align:justify
        #     🔴 Codex 阻断项6：线上态同样阻断，不再降级为警告。
        root_section = re.search(r'<section\b[^>]*>', html_content, re.IGNORECASE)
        root_ok = bool(root_section) and 'text-align:justify' in _style_of(root_section.group(0))
        if not root_ok:
            state_note = "（线上态）" if is_online_state else ""
            errors.append(
                f"【正文两端对齐缺陷】{state_note}最外层根 <section> 缺少 "
                f"text-align:justify 样式，移动端排版右侧将产生参差不齐锯齿！")
        else:
            print("    ✅ 正文两端对齐审查通过，全篇开启 text-align:justify 出版级对齐！")

        # =========================================================
        # 批次 2 · Gate 12-16（开篇变体与叙事风控 · 2026-10-13）
        # 说明：Gate 13/15/16 初始为 warn 级（按批准方案），
        #       跑够样本收集误报率后再决定是否升级为阻断。
        # =========================================================
        try:
            from scripts.risk_scan import load_lexicon, scan as risk_scan, summarize as risk_sum
            _has_risk = True
        except Exception:
            try:
                sys.path.insert(0, str(ROOT_DIR / "scripts"))
                from risk_scan import load_lexicon, scan as risk_scan, summarize as risk_sum
                _has_risk = True
            except Exception as _e:
                _has_risk = False
                print("    ⚠️ 风控引擎未就绪（%s），Gate 13-15 跳过" % _e)

        if _has_risk:
            lex = load_lexicon()
            rs = risk_scan(html_content, title_str, lex)

            # ---- Gate 13 叙事层风控（warn）----
            if rs["narrative"]:
                for it13 in rs["narrative"]:
                    warns.append("【Gate13 叙事层】命中「%s」（%s）→ 建议改为「%s」 %s"
                                 % (it13["word"], it13["label"],
                                    it13["safe_equivalent"], it13["ctx"][:60]))
                print("    ⚠️ Gate13 叙事层风控：命中 %d 处（警告级）" % len(rs["narrative"]))
            else:
                print("    ✅ Gate13 叙事层审查通过，五类危险叙事零命中！")

            # ---- Gate 14 身份越界（block）----
            if rs["identity"]:
                for it14 in rs["identity"]:
                    errors.append("【Gate14 身份越界】命中「%s」！Jackie 统一为「行业研究员」"
                                  "客观视角，严禁自称一线人员/从业者 %s"
                                  % (it14["word"], it14["ctx"][:60]))
            else:
                print("    ✅ Gate14 身份审查通过，零一线身份越界表述！")

            # ---- Gate 15 文学化虚构嫌疑（warn）----
            if rs["literary"]:
                for it15 in rs["literary"]:
                    warns.append("【Gate15 文学化虚构嫌疑】「%s」需核对真实出处，否则改写 "
                                 "%s" % (it15["word"], it15["ctx"][:60]))
                print("    ⚠️ Gate15 文学化细节：命中 %d 处（警告级，需核对出处）"
                      % len(rs["literary"]))
            else:
                print("    ✅ Gate15 审查通过，零文学化虚构嫌疑表述！")

        # ---- Gate 12 开篇变体声明（block）----
        if content_json_path.exists():
            try:
                cdata12 = json.loads(content_json_path.read_text(encoding="utf-8"))
                variant = cdata12.get("opening_variant", "")
                if not variant:
                    errors.append("【Gate12 开篇变体未声明】content.json 缺少 opening_variant "
                                  "字段！请按 references/opening_variants.json 声明 V1~V5，"
                                  "否则无法纳入变体效果统计。")
                else:
                    vp = ROOT_DIR / "references" / "opening_variants.json"
                    if vp.exists():
                        vlib = json.loads(vp.read_text(encoding="utf-8"))
                        vdef = vlib.get("variants", {}).get(variant)
                        if not vdef:
                            errors.append("【Gate12 变体代号无效】opening_variant=%s 未在 "
                                          "opening_variants.json 中定义！" % variant)
                        else:
                            lo, hi = vdef["cn_range"]
                            bq = re.search(r"<blockquote[^>]*>(.*?)</blockquote>",
                                           html_content, re.S)
                            target = bq.group(1) if bq else (
                                re.search(r"<p[^>]*>(.*?)</p>", html_content, re.S) or
                                [None, ""])[1] if bq is None else ""
                            lead_cn = count_chinese(target or "")
                            if target and not (lo <= lead_cn <= hi):
                                warns.append("【Gate12 变体长度】%s(%s) 开篇 %d 汉字，"
                                             "建议区间 %d~%d" % (variant, vdef["name"],
                                                                lead_cn, lo, hi))
                                print("    ⚠️ Gate12 变体 %s 开篇长度 %d 字（建议 %d~%d）"
                                      % (variant, lead_cn, lo, hi))
                            else:
                                print("    ✅ Gate12 开篇变体已声明：%s %s（开篇 %d 汉字）"
                                      % (variant, vdef["name"], lead_cn))
            except Exception as _e:
                print("    ⚠️ Gate12 检查异常: %s" % _e)

        # ---- Gate 16 变体轮换（warn，防算法聚类）----
        # 历史来源必须是可追溯的已推送记录，而不是一个从未被写入的 variant_history 键。
        try:
            meta_p = ROOT_DIR / "references" / "sync_meta.json"
            cj_p = target_dir / "content.json"
            if meta_p.exists() and cj_p.exists():
                _v = json.loads(cj_p.read_text(encoding="utf-8")).get("opening_variant")
                if _v:
                    _items = json.loads(meta_p.read_text(encoding="utf-8")).get("items", {})
                    # 排除本文自身，按 pushed_at 倒序取最近 2 篇已记录变体的稿件。
                    peers = [
                        (rec.get("pushed_at") or "", rec.get("opening_variant"))
                        for slug, rec in _items.items()
                        if slug != target_dir.name and rec.get("opening_variant")
                    ]
                    peers.sort(key=lambda x: x[0], reverse=True)
                    _recent = [v for _, v in peers[:2]]
                    if len(_recent) >= 2 and all(x == _v for x in _recent):
                        warns.append("【Gate16 变体轮换】变体 %s 已连续使用 2 篇，"
                                     "建议更换（防算法聚类判定框架一致）" % _v)
                        print("    ⚠️ Gate16 变体轮换警告：%s 已连续 2 篇" % _v)
                    elif not _recent:
                        print("    ⚠️ Gate16 无历史变体记录（unknown），不做通过判定")
                    else:
                        print("    ✅ Gate16 变体轮换审查通过（最近 %s）"
                              % ("、".join(_recent) if _recent else "无记录"))
                else:
                    print("    ⚠️ Gate16 缺少 opening_variant（unknown）")
        except Exception as _e16:
            print("    ⚠️ Gate16 检查异常: %s" % _e16)

        # =========================================================
        # 批次 4 · Gate 17-18（资产配图与封禁口号绝育门禁 · 2026-10-16 铁令）
        # =========================================================
        # ---- Gate 17 视觉资产与正文配图完整性门禁 (block) ----
        img_tags = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_content)
        broken_imgs = []
        for src in img_tags:
            if not src.startswith(('http://', 'https://')):
                # 归一化后必须仍在文章目录内，杜绝 photos/../../ 越界读（JOS-009 子项）。
                rel = src.replace('\\', '/').lstrip('./')
                local_img_p = (target_dir / rel).resolve()
                if not local_img_p.is_relative_to(target_dir):
                    broken_imgs.append(f"配图路径越界: {src}")
                elif not local_img_p.exists():
                    broken_imgs.append(f"本地图片不存在: {src}")
                elif local_img_p.stat().st_size < 5000:
                    broken_imgs.append(f"本地图片损坏或过小 ({local_img_p.stat().st_size} bytes): {src}")
            else:
                if not (src.startswith('http://mmbiz.qpic.cn/') or src.startswith('https://mmbiz.qpic.cn/')):
                    broken_imgs.append(f"非合法微信 CDN 链接: {src}")
        if broken_imgs:
            for bi in broken_imgs:
                errors.append(f"【Gate 17 配图完整性致命错误】{bi}！严禁包含失效配图或未上传本地图发布！")
        else:
            print(f"    ✅ Gate 17 视觉资产与配图审查通过（共 {len(img_tags)} 张配图全部有效）！")

        # ---- Gate 17b 配图数量下限（Codex JOS-040）----
        #     AGENTS.md §17 明令「Track A/D 正文配图严禁低于 6~8 张」，
        #     但此前代码只校验有效性、不校验数量——文档写了却没人执行。
        #     这里把文档要求变成硬门禁。
        # 🔴 Codex B06：配图要求必须由 references/track_registry.json 单一提供，
        #    不能在 pipeline 里再硬编码一份（否则与 AGENTS/测试必然再次分叉）。
        # 🔴 依据 docs/ARTICLE_VISUAL_JUDGMENT.md (2026-10-28):
        #    逐篇视觉判断规范明确覆盖旧规范中固定配额要求（形式没有固定配额，宁缺毋滥）。
        #    若文章启用专用 layout_profile 或进行逐篇视觉判断，严禁为了凑数塞不匹配的虚假素材，
        #    配图以真实匹配与有效性为准，豁免通用 6 张硬配额（仍需 >= 1 张有效配图）。
        try:
            sys.path.insert(0, str(ROOT_DIR / "scripts"))
            from track_policy import min_images as _min_images
            MIN_IMAGES = _min_images()
        except Exception:
            MIN_IMAGES = {"A": 6, "D": 6, "F": 6}

        has_custom_visual = False
        if source_path.exists():
            try:
                _cdata = json.loads(source_path.read_text(encoding='utf-8'))
                if _cdata.get("layout_profile") or (target_dir / "review" / "visual_direction.md").exists() or (target_dir / "visual_direction.md").exists():
                    has_custom_visual = True
            except Exception:
                pass
        if not has_custom_visual and ('solar_editorial' in html_content or 'custom_visual_judgment' in html_content):
            has_custom_visual = True

        if has_custom_visual:
            if len(img_tags) < 1:
                errors.append(f"【Gate 17b 配图数量不足】本篇虽启用逐篇视觉判断，但正文必须包含至少 1 张真实有效配图！")
            else:
                print(f"    ✅ Gate 17b 逐篇视觉判断生效：依据 ARTICLE_VISUAL_JUDGMENT 豁免通用固定配额，当前包含 {len(img_tags)} 张真实核验配图（宁缺毋滥）")
        elif track in MIN_IMAGES:
            need = MIN_IMAGES[track]
            if len(img_tags) < need:
                errors.append(
                    f"【Gate 17b 配图数量不足】轨道 {track} 正文仅 {len(img_tags)} 张配图，"
                    f"军规要求不少于 {need} 张（平均每 350~450 汉字一张实景或公文截图）！")
            else:
                print(f"    ✅ Gate 17b 配图数量合规（{len(img_tags)} >= {need}）")

        # ---- Gate 18 文末封禁口号终身绝育门禁 (block) ----
        banned_slogans = [
            "当技术被写成神话",
            "我们去翻原始出处与一线账本",
            "在土地、水流与代码之间，为现代产业留一份清醒的观察坐标",
        ]
        # 🔴 Codex JOS-014：旧实现直接 `s in html_content`，导致三类漏检：
        #    1) 禁句被 <span>/<strong> 等标签拆开；
        #    2) 只写了短版（如「当技术被写成神」）；
        #    3) 机械签名「—— jackie」不在名单里。
        #    这里改为「先归一化成纯文本再匹配」，并补充短句变体与签名规则。
        _slogan_text = re.sub(r'<[^>]+>', '', html_content)
        _slogan_text = (_slogan_text.replace('\u200b', '')
                        .replace('\ufeff', '').replace('\xa0', ' ')
                        .replace('\n', '').replace('\r', '').replace(' ', ''))
        found_banned = [s for s in banned_slogans if s.replace(' ', '') in _slogan_text]
        # 短句变体：只要出现禁句的高辨识片段也算命中（防「缩写规避」）
        banned_fragments = [
            "当技术被写成神",
            "翻原始出处与一线账本",
            "为现代产业留一份清醒的观察坐标",
        ]
        found_frag = [s for s in banned_fragments if s.replace(' ', '') in _slogan_text]
        # 机械署名：整行仅剩「—— jackie」这类固定落款
        if re.search(r'(——|--)\s*jackie\s*$', _slogan_text, re.I):
            found_frag.append('—— jackie（机械署名）')
        found_banned = sorted(set(found_banned + found_frag))
        if found_banned:
            errors.append(f"【Gate 18 封禁口号致命拦截】正文中检测到已永久封禁的机械化文末口号: {found_banned}！严禁在模板或正文中添加该口号！")
        else:
            print("    ✅ Gate 18 文末口号审查通过，零已封禁机械化模板口号！")

        # ---- Gate 19 具名主体可核（R11 新增；首周 warn，观察后升 block）----
        # 动因：编造「李德福」这类新具名角色此前没有任何 block 级门禁能拦。
        try:
            _cd = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cd = {}
        from scripts.editorial_claims import named_entity_errors
        errors.extend(named_entity_errors(_cd, target_dir))
        _g19 = named_entity_warnings(_cd, html_content)
        warns.extend(_g19)
        if _g19:
            print("    ⚠️ Gate 19：%d 项具名主体待登记" % len(_g19))
        else:
            print("    ✅ Gate 19 已登记证据契约通过；引语启发式未检出待登记项")

        # ---- Gate 20 标题证据与比较口径（block）----
        # 标题长短及两个数字的裸比值不判断真实性或传播效果。
        _g20 = title_mode_errors(_cd, target_dir)
        errors.extend(_g20)
        if _g20:
            print("    ❌ Gate 20 标题模式未通过（%d 项）" % len(_g20))
        else:
            print("    ✅ Gate 20 标题模式审查通过（%s）"
                  % str(_cd.get("title_mode") or "analysis"))

        # ---- Gate 21 配图图注时间脱敏（block）----
        # 规则：展示给读者的图注不得出现具体年份/月份（正文来源年份不在此列）。
        _g21 = caption_time_errors(_cd)
        errors.extend(_g21)
        if _g21:
            print("    ❌ Gate 21 配图图注含具体年份/月份（%d 条）" % len(_g21))
        else:
            print("    ✅ Gate 21 配图图注时间脱敏通过（图注不含具体年/月）")

    print("\n------------------------------------------")
    if warns:
        print(f"⚠️ 警告级提示 {len(warns)} 条（不阻断发布）:")
        for w in warns:
            # 警告使用独立前缀，避免与阻断项混淆（JOS-016：回归脚本曾把警告当失败）。
            print(f"   ~ {w}")
    if errors:
        print(f"❌ 质检门禁未通过！发现 {len(errors)} 项严重问题:")
        for e in errors:
            print(f"   - {e}")
        return False
    else:
        # 🔴 Codex 阻断项3：check 通过时落盘门禁回执，
        #    remote-deploy 必须凭此回执才能执行，且回执绑定 article.html 指纹。
        try:
            # 🔴 Codex B02：回执必须携带**足以证明「门禁真的通过」**的完整状态，
            #    否则任何能拼出匹配哈希的文件都能冒充「已通过」。
            #    这里补齐：passed、error_count、content.json 指纹、
            #    封面指纹、图片资产指纹、轨道、政策版本。
            _cover = target_dir / 'cover_900x383.jpg'
            # 🔴 Codex 第三轮 B02：资产清单必须来自**正文实际引用**，
            #    不能用 photos/* 目录代替（cards/*.png 等会被漏掉）。
            _html_txt = (target_dir / 'article.html').read_text(encoding='utf-8') \
                if (target_dir / 'article.html').is_file() else ''
            _refs = _referenced_local_assets(_html_txt, target_dir)
            _imgs_sha = _assets_fingerprint(_refs, target_dir)
            _imgs = _refs
            _cj = target_dir / 'content.json'
            receipt = {
                'dir': target_dir.name,
                'track': track,
                'passed': True,
                'article_sha256': _sha256_bytes(
                    (target_dir / 'article.html').read_bytes()),
                'content_sha256': _sha256_bytes(_cj.read_bytes())
                if _cj.is_file() else '',
                'cover_sha256': _sha256_bytes(_cover.read_bytes())
                if _cover.is_file() else '',
                'assets_sha256': _imgs_sha,
                'asset_count': len(_imgs),
                'policy_version': CURRENT_POLICY_VERSION,
                'cdn_mapping_sha256': __import__('scripts.asset_contract', fromlist=['mapping_snapshot']).mapping_snapshot(target_dir, _refs)[0],
                'generated_at': __import__('datetime').datetime.now(
                    __import__('datetime').timezone.utc).isoformat(),
                'warn_count': len(warns),
                'error_count': 0,
            }
            receipt_path = target_dir / '.studio' / 'gate_receipt.json'
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"    📝 门禁回执已落盘: {receipt_path}")
        except Exception as _e:
            print(f"❌ 门禁回执无法生成：{_e}")
            return False
        print("🎉 恭喜！全项指标 100% 通过 Pre-Flight 门禁！")
        return True

# -------------------------------------------------------------
# 3. 腾讯云一键直连发布 (Deploy)
# -------------------------------------------------------------
def cmd_deploy(args):
    """旧版直连部署入口（已降级为 release 的受控别名，JOS-007）。

    AGENTS.md §11⑤ 规定 `release` 是唯一发布入口，但历史上 `deploy` 是另一条
    独立链路：不做 drift 检测、不写台账指纹、不做独立回读。两条入口并存会让
    「本地为准」的保证出现缺口。

    现在 deploy 先跑本地门禁（保证无网也能阻断），再转调 release 完成
    同样的 check → diff → push → verify → audit。需要旧行为的场景应显式用 `push`。
    """
    print("\n[*] deploy 已并入 release 统一链路（JOS-007）；"
          "执行 check → diff → push → verify → audit")
    # 先做纯本地门禁：不联网即可失败，避免为了发现明显问题还要先建远端连接。
    if not cmd_check(args):
        raise ValueError("前置门禁未通过，终止部署")
    return cmd_release(args)


def _cmd_deploy_legacy(args):
    """保留旧实现以便审计对照；CLI 不再暴露此路径。"""
    target_dir = Path(args.dir).resolve()
    track = args.track.upper()
    mode = args.mode.lower()
    media_id = args.media_id

    # 1. 前置质检必须先通过（业务层抛错，由 CLI 顶层转退出码）
    if not cmd_check(args):
        raise ValueError("前置门禁未通过，终止部署")

    print(f"\n==========================================")
    print(f"🚀 开始执行腾讯云一键直连部署 (Mode={mode})")
    print(f"📂 本地目录: {target_dir}")
    print(f"==========================================\n")

    rel_name = target_dir.name
    remote_base = f"/opt/wechat-publisher"

    ssh_cfg = str(Path.home() / ".ssh" / "config").replace('\\', '/')
    # 2. 同步 pipeline.py 及本地工程到云端（确立本地为唯一事实源）
    local_pipe = Path(__file__).resolve()
    print(f"[*] 正在同步核心调度脚本 pipeline.py 至云端...")
    cmd_sync_pipe = f'scp -F "{ssh_cfg}" "{local_pipe}" your-remote-vps:/opt/wechat-publisher'
    res_pipe = os.system(cmd_sync_pipe)
    if res_pipe != 0:
        raise RuntimeError("核心脚本 pipeline.py 同步云端失败，请检查 SSH 连接！")

    # 版本一致性校验断言 (Gate: Pipeline SHA256 Sync)
    local_sha = hashlib.sha256(local_pipe.read_bytes()).hexdigest()
    verify_cmd = f'ssh -F "{ssh_cfg}" your-remote-vps "sha256sum /opt/wechat-publisher"'
    proc = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True)
    remote_sha = proc.stdout.split()[0] if proc.stdout else ""
    if local_sha != remote_sha:
        raise RuntimeError(f"致命错误：云端 pipeline.py 指纹与本地不一致！\n本地: {local_sha}\n云端: {remote_sha}")
    print(f"✅ pipeline.py 两端指纹严格一致 (SHA256: {local_sha[:8]}...)")

    print(f"[*] 正在将本地目录同步至腾讯云: {remote_base} ...")
    cmd_scp = f'scp -F "{ssh_cfg}" -r "{target_dir}" your-remote-vps:/opt/wechat-publisher'
    res = os.system(cmd_scp)
    if res != 0:
        raise RuntimeError("SCP 同步失败，请检查 SSH 连接与 ~/.ssh/config 配置！")
    print("✅ 文件同步完成！")

    # 3. 远程执行上传脚本
    print(f"[*] 正在云端调用微信官方 API 执行草稿入库...")
    media_arg = f'--media-id "{media_id}"' if media_id else ''
    ssh_cmd = f'ssh -F "{ssh_cfg}" your-remote-vps "python3 /opt/wechat-publisher remote-deploy --dir {remote_base} --track-type {args.track_type} --mode {mode} {media_arg}"'
    res2 = os.system(ssh_cmd)
    if res2 != 0:
        raise RuntimeError("云端微信 API 入库失败，请检查报错！")

    # 4. 回传云端生成的 cdn_urls.json 与 article_cdn.html 至本地对应目录（闭环回流）
    print(f"[*] 正在将云端生成的 cdn_urls.json 和 article_cdn.html 回传至本地对应目录...")
    pull_files = ["cdn_urls.json", "article_cdn.html"]
    pulled_ok = True
    for fname in pull_files:
        cmd_pull = f'scp -F "{ssh_cfg}" your-remote-vps:"{remote_base}/{fname}" "{target_dir}/{fname}"'
        res_pull = os.system(cmd_pull)
        if res_pull != 0:
            pulled_ok = False
            print(f"⚠️ 警告：回传 {fname} 失败！")
    if pulled_ok:
        print("✅ 云端 CDN 资产 (cdn_urls.json, article_cdn.html) 已成功回传本地，两端保持 100% 同步！")

    print("\n🎉 微信公众平台草稿部署与回读核验 100% 成功！")

def cmd_remote_deploy(args):
    target_dir = Path(args.dir).resolve()

    # 🔴 Codex 阻断项3：这是最容易绕过全部门禁的生产入口。
    #    三重强制：
    #      1) 必须有上游 check 通过后落盘的回执（含目录名与内容指纹）；
    #      2) 回执指纹必须与当前 article.html 一致（防止门禁后改稿）；
    #      3) 必须通过写授权守卫（防止误跑即写线上）。
    from scripts.write_guard import require_write_authorization
    require_write_authorization("云端落库 remote-deploy")

    receipt_path = getattr(args, 'gate_receipt', '') or ''
    if not receipt_path:
        raise ValueError(
            "remote-deploy 必须携带 --gate-receipt（由 check 通过后生成）；"
            "禁止无门禁回执直接落库")
    receipt_file = Path(receipt_path).resolve()
    if not receipt_file.is_file():
        raise FileNotFoundError("门禁回执不存在: %s" % receipt_file)
    receipt = json.loads(receipt_file.read_text(encoding='utf-8'))

    # 🔴 Codex B02：旧实现只核对 dir 与 article_sha256，
    #    于是「passed=false、error_count=99、track=B」的回执照样能进写入边界。
    #    现在逐项校验「回执是否真的代表门禁通过」，任一不符即拒绝，
    #    且全部检查在任何平台写入之前完成。
    def _receipt_require(key, expected, label):
        got = receipt.get(key)
        if got != expected:
            raise ValueError(
                "门禁回执校验失败（%s）：回执=%r，期望=%r。"
                "该回执不能证明门禁通过，拒绝落库。" % (label, got, expected))

    if receipt.get('passed') is not True:
        raise ValueError(
            "门禁回执校验失败：passed=%r（必须为 true）。"
            "未通过门禁的回执禁止用于落库。" % receipt.get('passed'))
    # 🔴 Codex 第三轮 B02：旧实现 `int(receipt.get('error_count') or 0) == 0`
    #    把「字段缺失」当成 0 通过。缺失必须拒绝，不能自动解释为通过。
    if 'error_count' not in receipt:
        raise ValueError(
            "门禁回执校验失败：缺少 error_count 字段。"
            "字段缺失不等于 0，拒绝落库。")
    if type(receipt.get('error_count')) is not int or receipt['error_count'] != 0:
        raise ValueError(
            "门禁回执校验失败：error_count=%r（必须为 0）"
            % receipt.get('error_count'))
    # 🔴 policy_version 必须存在且与当前政策版本一致（缺失/过期都拒绝）
    if 'policy_version' not in receipt:
        raise ValueError(
            "门禁回执校验失败：缺少 policy_version 字段，无法确认政策版本，拒绝落库。")
    _receipt_require('policy_version', CURRENT_POLICY_VERSION, '政策版本')
    _receipt_require('dir', target_dir.name, '目标目录')
    _receipt_require('track', str(args.track).upper(), '内容轨道')

    html_p = target_dir / 'article.html'
    if not html_p.is_file():
        raise FileNotFoundError("缺少 article.html: %s" % html_p)
    _receipt_require('article_sha256', _sha256_bytes(html_p.read_bytes()),
                     '正文指纹')

    _cj_p = target_dir / 'content.json'
    _cj_actual = _sha256_bytes(_cj_p.read_bytes()) if _cj_p.is_file() else ''
    _receipt_require('content_sha256', _cj_actual, 'content.json 指纹')

    _cover_p = target_dir / 'cover_900x383.jpg'
    _cover_actual = (_sha256_bytes(_cover_p.read_bytes())
                     if _cover_p.is_file() else '')
    _receipt_require('cover_sha256', _cover_actual, '封面指纹')

    # 图片资产指纹：覆盖「检查后换图、正文哈希未变」这一未绑定状态
    # 🔴 Codex 第三轮 B02：部署侧同样必须按**正文实际引用**校验资产，
    #    不能只看 photos/*（cards/*.png 被换会漏检）。
    _imgs = _referenced_local_assets(html_p.read_text(encoding='utf-8'), target_dir)
    _imgs_hatch = _assets_fingerprint(_imgs, target_dir)
    _receipt_require('assets_sha256', _imgs_hatch, '图片资产指纹')
    _receipt_require('asset_count', len(_imgs), '图片数量')
    from scripts.asset_contract import mapping_snapshot, confined_asset, replace_sources, cdn_url, image_sources
    mapping_sha, checked_cdn = mapping_snapshot(target_dir, _imgs)
    _receipt_require('cdn_mapping_sha256', mapping_sha, 'CDN映射指纹')
    if args.mode not in ('new', 'update') or (args.mode == 'update' and not args.media_id):
        raise ValueError('部署模式无效或update缺少media_id')

    from scripts.production_review import review_errors as production_errors
    stage_errors = production_errors(target_dir)
    if stage_errors:
        raise ValueError('生产阶段门禁：' + '; '.join(stage_errors))


    print("  ✅ 门禁回执校验通过：passed=true / 0 error / 轨道 %s / 正文+content+封面+%d 张图指纹全绑定"
          % (receipt.get('track'), len(_imgs)))

    print(f"\n[*] 云端就绪: 开始执行草稿落库, 目录={target_dir}")
    sys.path.insert(0, '/opt/wechat-publisher')
    from scripts import wechat_api

    cover_p = target_dir / 'cover_900x383.jpg'
    if not cover_p.exists():
        raise FileNotFoundError(f"Missing cover: {cover_p}")
    thumb_id = wechat_api.upload_thumb_image(str(cover_p))
    print(f"[*] 封面上传成功, thumb_media_id={thumb_id}")

    cdn_f = target_dir / 'cdn_urls.json'
    cdn = dict(checked_cdn)
    html_src = html_p.read_text(encoding='utf-8')
    aliases = {}
    # The exact checked reference set is also the only upload set.
    for rel in _imgs:
        p = confined_asset(target_dir, rel)
        if rel not in cdn:
            cdn[rel] = wechat_api.upload_content_image(str(p))
        if not cdn_url(cdn[rel]):
            raise ValueError('上传返回了非微信CDN链接')
    for src in image_sources(html_src):
        if not cdn_url(src):
            canonical = confined_asset(target_dir, src).relative_to(target_dir).as_posix()
            aliases[src] = cdn[canonical]
    html = replace_sources(html_src, aliases)
    cdn_f.write_text(json.dumps(cdn, ensure_ascii=False, indent=2), encoding='utf-8')

    # 部署前刚性断言门禁：检查所有 <img> 标签的 src 是否为合法未污染的微信 CDN URL
    img_matches = image_sources(html)
    if (len(img_matches) != len(image_sources(html_src))
            or not all(cdn_url(src) for src in img_matches)):
        raise ValueError('CDN图片数量或地址核验失败')
    print(f"[*] CDN 替换核验: {len(img_matches)} 个真实图片引用全部有效")

    (target_dir / 'article_cdn.html').write_text(html, encoding='utf-8')
    print(f"[*] 云端资产已落盘: cdn_urls.json ({len(cdn)} 个映射), article_cdn.html ({len(html)} 字符)")

    c_json = json.loads((target_dir / 'content.json').read_text('utf-8'))
    art = {
        'title': c_json.get('title'),
        'author': 'Jackie',
        'digest': c_json.get('digest', ''),
        'content': html,
        'thumb_media_id': thumb_id,
        'show_cover_pic': 1,
        'need_open_comment': 0,
        'only_fans_can_comment': 0,
        'content_source_url': ''
    }

    m_id = None
    if args.mode == 'update' and args.media_id:
        wechat_api.update_draft(args.media_id, art)
        m_id = args.media_id
        print(f"[*] 成功更新草稿: {m_id}")
    else:
        m_id = wechat_api.add_draft([art])
        print(f"[*] 成功新建草稿: {m_id}")

    reg_f = Path('/tmp/j004j005/cover_registry.json')
    reg = json.loads(reg_f.read_text('utf-8')) if reg_f.exists() else {}
    # 注册时间必须是本次真实事件时间（JOS-038），不是写死的某天。
    from datetime import datetime as _dt, timezone as _tz
    reg[m_id] = {'title': art['title'], 'thumb_media_id': thumb_id,
                 'track': args.track_type,
                 'time': _dt.now(_tz.utc).astimezone().isoformat()}
    reg_f.write_text(json.dumps(reg, ensure_ascii=False, indent=2), 'utf-8')

    res_draft = wechat_api.get_draft(m_id)
    it = res_draft['news_item'][0]
    ret_title = it.get('title', '')
    ret_content = it.get('content', '')
    ret_thumb = it.get('thumb_media_id', '')
    has_escaped = '&lt;section' in ret_content or '&lt;blockquote' in ret_content
    print(f"🎉 验证完成: media_id={m_id} | 标题={ret_title}")
    print(f"    - 正文长度: {len(ret_content)} 字符")
    print(f"    - 封面素材ID: {ret_thumb or '❌ 空值!'}")
    print(f"    - 实体乱码检测: {'❌ 异常: 存在转义乱码!' if has_escaped else '✅ 纯净原生富文本，零实体乱码！'}")
    if not ret_thumb:
        raise RuntimeError(f"致命错误：草稿落库后 thumb_media_id 为空！封面未成功绑定！media_id={m_id}")
    if has_escaped:
        raise RuntimeError("致命错误：草稿落库后检测到 HTML 实体代码！")
    banned_in_remote = [s for s in ["当技术被写成神话", "为现代产业留一份清醒的观察坐标"] if s in ret_content]
    if banned_in_remote:
        raise RuntimeError(f"致命错误：草稿落库后检测到已封禁的模板口号: {banned_in_remote}")

# -------------------------------------------------------------
# 4. 配图工作台按需微调 (Edit-images)
# -------------------------------------------------------------
def cmd_edit_images(args):
    target_dir = Path(args.dir).resolve()
    print(f"\n[*] 启动配图工作台微调服务: 目标目录={target_dir}")
    print("[*] 正在拉起本地轻量 Web 工作台: http://localhost:8765 ...")
    os.system(f'python "{ROOT_DIR}/tools/image_picker/server.py" --dir "{target_dir}"')


# =============================================================
# 批次 1 · 交付层防漂移（2026-10-13）
# =============================================================
SSH = os.environ.get("JACKIE_SSH_HOST", "opc")
CURRENT_POLICY_VERSION = '2026-10-19'
from scripts.ssh_transport import transport_args, remote_root, remote_python
REMOTE_ROOT = remote_root()
OPS = "scripts/mp_ops.py"
MARK = "<<<OPSJSON>>>"
SYNC_META = ROOT_DIR / "references" / "sync_meta.json"


def _sha(t):
    import hashlib
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def load_sync_meta():
    if SYNC_META.exists():
        try:
            return json.loads(SYNC_META.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": {}}


def remote_ops(subcmd_args, timeout=300, write_reason=None):
    """通过 ssh 调用服务端 mp_ops.py，解析 JSON 标记。

    注意：ssh 会把远端 stderr 转发到本地 stderr，可能带杂音。
    因此**优先只解析 stdout**，找不到再回退到合并流。
    """
    import subprocess
    import shlex
    argv = shlex.split(subcmd_args) if isinstance(subcmd_args, str) else list(subcmd_args)
    remote_argv = [remote_python(), OPS, *argv]
    if argv and argv[0] == 'push':
        if write_reason is None:
            # Direct legacy calls require explicit process environment. File
            # credentials need complete binding and are consumed by cmd_push.
            from scripts.write_guard import require_write_authorization, REASON_ENV
            require_write_authorization('远端草稿写入')
            reason = os.environ.get(REASON_ENV, '').strip()
        else:
            reason = str(write_reason).strip()
        if not reason:
            from scripts.write_guard import WriteNotAuthorized
            raise WriteNotAuthorized('远端写入缺少当前操作的明确理由')
        remote_argv = ['env', 'JACKIE_ALLOW_WRITE=1', 'JACKIE_WRITE_REASON=' + reason, *remote_argv]
    inner = "cd " + shlex.quote(REMOTE_ROOT) + " && " + shlex.join(remote_argv)
    p = subprocess.run(transport_args('ssh') + [SSH, inner],
                       capture_output=True, timeout=timeout)
    candidates = [(p.stdout or b"").decode("utf-8", "replace")]
    candidates.append(((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace"))

    for raw in candidates:
        if MARK not in raw:
            continue
        payload = raw.split(MARK, 1)[1].strip()
        for line in payload.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line), raw, p.returncode
                except Exception:
                    continue
        try:
            return json.loads(payload), raw, p.returncode
        except Exception:
            continue
    raw = candidates[1]
    return None, "未找到有效 JSON 输出\n%s" % raw[:800], p.returncode


def find_dir_by_media_id(mid):
    meta = load_sync_meta()
    for d, m in meta.get("items", {}).items():
        if m.get("media_id") == mid:
            return ROOT_DIR / "generated" / d, m
    return None, None


def cmd_diff(args):
    """漂移检测：比对本地指纹与线上指纹。

    --both：启用**双向比对**（本地 ↔ 线上 ↔ 台账），
            可发现「本地改了没推」这类单向 diff 抓不到的缺口。
    """
    if getattr(args, "both", False):
        return cmd_diff_both(args)

    print("\n" + "=" * 60)
    print("🔍 漂移检测 (diff)")
    print("=" * 60)
    meta = load_sync_meta()
    data, raw, rc = remote_ops("list")
    if not data or not data.get("ok"):
        print("❌ 拉取线上列表失败：%s" % (raw or "")[:300])
        sys.exit(1)

    online = {i["media_id"]: i for i in data["items"]}
    only = args.media_id or None
    rows = []
    for d, m in meta.get("items", {}).items():
        mid = m.get("media_id")
        if only and mid != only:
            continue
        o = online.get(mid)
        if not o:
            rows.append((d, "❔ 线上已删除", m.get("title", "")))
            continue
        if o["content_sha256"] == m.get("content_sha256") and \
           o["title_sha256"] == m.get("title_sha256"):
            rows.append((d, "🟢 一致", o["title"][:40]))
        elif o["title_sha256"] != m.get("title_sha256"):
            rows.append((d, "🔴 标题分叉", "%s → %s" % (m.get("title", "")[:24],
                                                    o["title"][:24])))
        else:
            rows.append((d, "🟠 正文漂移（线上被手改）", o["title"][:40]))

    for d, st, t in rows:
        print("  %-46s %s" % (d[:46], st))
        if "分叉" in st or "漂移" in st:
            print("        %s" % t)
    n_bad = sum(1 for _, s, _ in rows if "一致" not in s)
    print("\n  共 %d 篇；非一致 %d 篇" % (len(rows), n_bad))
    if n_bad:
        print("  ⚠️ 检测到漂移 —— push 前需人工裁决（--force 才能覆盖）")
    # R11：旧写法 `return 0 if n_bad == 0 else 0` 恒为 0，
    # 于是任何脚本化调用都无法从退出码发现漂移，只能靠人眼读打印。
    return 0 if n_bad == 0 else 3


def cmd_diff_both(args):
    """双向漂移检测：本地 ↔ 线上 ↔ 台账。

    实现放在 scripts/ops/bidirectional_diff.py（本地运行，因为台账只在本地）。
    """
    import subprocess as _sp
    script = ROOT_DIR / "scripts" / "ops" / "bidirectional_diff.py"
    if not script.exists():
        print("❌ 未找到脚本: %s" % script)
        sys.exit(2)
    p = _sp.run([sys.executable, str(script)])
    sys.exit(p.returncode)


def cmd_verify(args):
    """独立回读断言。

    🔴 Codex B03：旧实现在 `<文章目录>/_push_payload.json` 找期望正文，
    而 push 已改为写 `.studio/push_runs/<run_id>/payload.json`——
    两者路径不通，导致 verify 要么拿不到本次 payload、要么捡到上次的残留文件。
    现在改为：调用方（push/release）**显式传入本次不可变 payload 的远端路径**；
    只有调用方没传时，才回退到旧路径，并明确告警「可能是残留」。
    """
    mid = args.media_id
    print("\n" + "=" * 60)
    print("🔎 独立回读断言 (verify)")
    print("=" * 60)
    tdir, _ = find_dir_by_media_id(mid)
    local_path = getattr(args, "payload_path", None)
    if local_path:
        print("  期望正文来源：本次操作 payload（%s）" % local_path)
    else:
        legacy = None
        if tdir:
            lp = tdir / "_push_payload.json"
            if lp.exists():
                legacy = f"{REMOTE_ROOT}/generated/{tdir.name}/{lp.name}"
        if legacy:
            print("  ⚠️ 未收到本次 payload 路径，回退到旧文件：%s" % legacy)
            print("     （该文件可能是上一次推送的残留，结论仅供参考）")
            local_path = legacy
        else:
            print("  ⚠️ 未收到本次 payload 路径，且无旧文件 → 正文等价性将判 unknown")
    data, raw, rc = remote_ops(
        "verify %s %s" % (mid, local_path or "-"))
    if not data:
        print("❌ 执行失败：%s" % (raw or "")[:400])
        sys.exit(1)

    info = data.get("info", {})
    print("  封面: %s | 汉字: %d | HTML: %d | 图片: %d 张"
          % ("有 ✅" if info.get("has_cover") else "无 ❌",
             info.get("cn_count", 0), info.get("html_len", 0),
             info.get("img_count", 0)))
    for w in data.get("warns", []):
        print("  ⚠️ %s" % w)
    for e in data.get("errors", []):
        print("  ❌ %s" % e)
    if data.get("ok"):
        print("\n🎉 verify 全项通过")
    else:
        print("\n❌ verify 未通过（%d 项错误）" % len(data.get("errors", [])))
    if rc != 0 or not data.get("ok"):
        raise RuntimeError("Independent verification failed")

    # R11（P2）：只有官方独立回读通过才写 draft_verified 回执。
    # 旧台账里出现的 draft_verified 全部是历史归档文本，不是本次回读结果；
    # 这里改为每次 push 后由真实 get_draft 断言落盘，并带上本次 payload 归属。
    target = tdir
    if target is None and getattr(args, "dir", None):
        target = Path(args.dir).resolve()
    if target is None:
        print("\n⚠️ 未能定位本篇目录，回执未落盘（不影响本次回读结论）")
        return data
    receipt = {
        "schema": 2,
        "media_id": mid,
        "thumb_media_id": info.get("thumb_media_id", ""),
        "cn_count": info.get("cn_count"),
        "img_count": info.get("img_count"),
        "html_len": info.get("html_len"),
        "has_cover": bool(info.get("has_cover")),
        "body_equivalent": info.get("body_equivalent"),
        "draft_verified": True,
        "verified_at": __import__('datetime').datetime.now(
            __import__('datetime').timezone.utc).isoformat(),
        "payload_path": local_path or "",
        "operation_scope": "official_readback_this_run_only_not_publication",
        "warns": list(data.get("warns", [])),
    }
    dest = target / "draft_meta.json"
    dest.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print("  ✅ 回执已落盘：draft_meta.json（draft_verified=true）")
    return data


def cmd_audit(args):
    """全库风控体检。"""
    print("\n" + "=" * 60)
    print("🩺 全库风控体检 (audit)")
    print("=" * 60)
    data, raw, rc = remote_ops("audit", timeout=420)
    if not data or not isinstance(data.get('rows'), list):
        print("❌ 执行失败：%s" % (raw or "")[:400])
        sys.exit(1)
    for row in data["rows"]:
        flag = "✅" if row["block"] == 0 else "❌"
        print("%s %s" % (flag, row["title"][:52]))
        if row["struct"]:
            print("      🔴 结构问题: %s" % "、".join(row["struct"]))
        if args.verbose:
            for ln in row["lines"]:
                print("      %s" % ln)
        elif row["block"] or row["warn"]:
            print("      🔴阻断 %d / ⚠️警告 %d" % (row["block"], row["warn"]))
    print("\n  共 %d 篇 | 阻断问题 %d | 警告 %d"
          % (data["total"], data["block_total"], data["warn_total"]))
    if data["ok"]:
        print("  🎉 全库零阻断问题")
    else:
        print("  ❌ 存在 %d 项阻断问题（加 -v 查看明细）" % data["block_total"])
    if rc != 0 or not data["ok"]:
        raise RuntimeError("Audit reported blocking findings")
    return data


def _sync_writing_review(target_dir):
    """Send current source and retained review even when all CDN URLs are cached."""
    from scripts.editorial_policy import writing_review_errors
    errors = writing_review_errors(target_dir)
    if errors:
        raise ValueError('正文编辑复核阻断：' + '; '.join(errors))
    root = Path(target_dir).resolve()
    review = json.loads((root / 'writing_review.json').read_text(encoding='utf-8'))
    evidence = (root / review['evidence_file']).resolve()
    from scripts.production_review import review_files
    files = list(dict.fromkeys([root / 'content.json', root / 'writing_review.json', evidence, *review_files(root)]))
    remotes = [REMOTE_ROOT + '/generated/' + root.name + '/' + source.relative_to(root).as_posix() for source in files]
    dirs = {str(__import__('posixpath').dirname(r)) for r in remotes}
    if dirs:
        py_dirs = repr(list(dirs))
        py_mkdir = f"import os; [os.makedirs(d, exist_ok=True) for d in {py_dirs}]"
        try:
            subprocess.run(transport_args('ssh') + [SSH, f"{remote_python()} -c \"{py_mkdir}\""],
                           check=True, capture_output=True, timeout=15)
        except Exception as exc:
            print(f"[*] 远端目录已就绪，跳过 mkdir: {exc}")
    for source, remote in zip(files, remotes):
        time.sleep(0.3)
        subprocess.run(transport_args('scp') + [str(source), SSH + ':' + remote],
                       check=True, capture_output=True, timeout=30)
    holds_file = ROOT_DIR / 'references' / 'editorial_holds.json'
    if holds_file.is_file():
        time.sleep(0.3)
        subprocess.run(transport_args('scp') + [str(holds_file), SSH + ':' + REMOTE_ROOT + '/references/editorial_holds.json'],
                       check=True, capture_output=True, timeout=30)


def cmd_push(args):
    """本地覆盖线上（强制先 check + diff）。"""
    target_dir = Path(args.dir).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", target_dir.name):
        raise ValueError("Unsafe production directory identifier")
    mid = args.media_id
    print("\n" + "=" * 60)
    print("📤 本地覆盖线上 (push)  mode=%s" % args.mode)
    print("=" * 60)

    # 1. 先跑门禁
    if not cmd_check(args):
        print("\n❌ 门禁未通过，中止 push")
        sys.exit(1)

    # Local syntax checks alone cannot approve writing, facts or source assets.
    from scripts.delivery_gate import check as strict_delivery_check
    strict = strict_delivery_check(target_dir, args.track, args.track_type)
    if not strict.get('passed'):
        raise RuntimeError('严格交付门禁未通过：' + '; '.join(strict.get('errors', [])))

    # Claim permission before any remote side effect, including CDN uploads.
    from scripts.write_guard import (consume_write_authorization,
                                     delivery_bundle_sha256, WriteNotAuthorized)
    checked_content_sha = _sha256_bytes((target_dir / 'content.json').read_bytes())
    checked_bundle_sha = delivery_bundle_sha256(target_dir)
    authorized, write_reason = consume_write_authorization(
        '草稿及素材写入', slug=target_dir.name, content_sha256=checked_content_sha,
        bundle_sha256=checked_bundle_sha, mode=args.mode, media_id=mid or '')
    if not authorized:
        raise WriteNotAuthorized('当前版本或操作缺少有效单次授权；请复核后重新 authorize')
    import uuid as _uuid
    run_id = "%s-%s" % (__import__('datetime').datetime.now().strftime('%Y%m%dT%H%M%S'),
                         _uuid.uuid4().hex[:8])
    run_dir = target_dir / '.studio' / 'push_runs' / run_id
    from scripts.delivery_snapshot import freeze_delivery_bundle
    # 冻结快照放到浅临时根下，但目录名必须保留稿件 slug（远端 scp 到 generated/ 依赖此名）。
    # 原因：Windows MAX_PATH(260) + LongPathsEnabled=0 时，深层 run 目录叠加 content-addressed
    # 长证据文件名，会让 Python is_file() 判否而误报“证据快照不存在或越界”。
    import tempfile as _tempfile
    _snap_root = Path(_tempfile.gettempdir()) / 'jackie_deliv'
    frozen_target = freeze_delivery_bundle(target_dir, _snap_root / run_id / target_dir.name,
                                            checked_bundle_sha)
    frozen_gate = strict_delivery_check(frozen_target, args.track, args.track_type)
    if not frozen_gate.get('passed'):
        raise ValueError('冻结交付包门禁未通过：' + '; '.join(frozen_gate.get('errors', [])))

    # 2. 漂移检测
    tdir, meta_item = find_dir_by_media_id(mid) if mid else (target_dir, None)
    if mid:
        ddata, raw, rc = remote_ops("list")
        if rc != 0 or not ddata or not ddata.get("ok"):
            raise RuntimeError("Cannot establish remote state; refusing overwrite")
        if not meta_item:
            raise RuntimeError("Missing trusted sync baseline")
        if ddata and ddata.get("ok"):
            o = next((i for i in ddata["items"] if i["media_id"] == mid), None)
            if not o:
                raise RuntimeError("Remote target is absent from verified inventory")
            if o and meta_item:
                # 台账字段缺失 ≠ 线上被手改：前者必须重建基线，--force 不能代替证据。
                missing = [k for k in ("content_sha256", "title_sha256")
                           if not meta_item.get(k)]
                if missing:
                    raise RuntimeError(
                        "台账缺少可信基线字段 %s，禁止覆盖线上；请先用 diff --both 或"
                        "受控回读重建 references/sync_meta.json 中该稿指纹" % missing)
                if any(not o.get(k) or o[k] != meta_item[k]
                       for k in ("content_sha256", "title_sha256")):
                    print("\n🟠 检测到线上漂移（线上内容或标题与台账不一致，可能被前台手改）")
                    if not args.force:
                        print("   ⛔ 已阻断。确认要覆盖请加 --force")
                        sys.exit(2)
                    print("   ⚠️ --force 已指定，继续覆盖")

        # 🔴 Codex 阻断项4b：内容与标题都跟台账一致时不应再推一次。
        #    旧实现会重复执行 add_draft/update_draft，既浪费也可能造成重复草稿。
        local_sha = _sha256_bytes((frozen_target / "article.html").read_bytes())
        if (meta_item.get("content_sha256") == local_sha
                and meta_item.get("title_sha256") == _sha(_read_title(target_dir))
                and o and o.get("content_sha256") == local_sha
                and not args.force):
            print("\n✅ 本地与线上均与台账一致（无变化），跳过推送")
            print("   如需强制重推，请加 --force")
            return {"ok": True, "skipped": "unchanged"}

    # 3. 组装 payload 并推送
    html_p = frozen_target / "article.html"
    cj_p = frozen_target / "content.json"
    if not html_p.exists() or not cj_p.exists():
        print("❌ 缺 article.html 或 content.json")
        sys.exit(1)
    cj = json.loads(cj_p.read_text(encoding="utf-8"))

    html_src = html_p.read_text(encoding="utf-8")
    from scripts.asset_contract import (image_sources, local_assets, mapping_snapshot,
                                       confined_asset, replace_sources, cdn_url)
    raw_imgs = image_sources(html_src)
    refs = local_assets(html_src, frozen_target)
    cdn_f = frozen_target / "cdn_urls.json"
    _, cdn_map = mapping_snapshot(frozen_target, refs)
    missing_cdn = [ref for ref in refs if ref not in cdn_map]

    if missing_cdn:
        print(f"[*] 检测到 {len(missing_cdn)} 个本地图片未同步微信 CDN: {missing_cdn}")
        print(f"[*] 正在自动同步本地图片与素材目录至腾讯云...")
        subprocess.run(transport_args('scp') + ["-r", str(frozen_target),
                        f"{SSH}:{REMOTE_ROOT}/generated/"],
                       check=True, capture_output=True, timeout=60)
        remote_script = f"""
import sys, os, json, time
sys.path.insert(0, '{REMOTE_ROOT}')
sys.path.insert(0, '{REMOTE_ROOT}/scripts')
from scripts import wechat_api
from scripts.asset_contract import confined_asset, cdn_url, local_assets, mapping_snapshot
from pathlib import Path

target_d = Path({json.dumps(REMOTE_ROOT + '/generated/')} + {json.dumps(target_dir.name)})
cdn_p = target_d / 'cdn_urls.json'
html = (target_d / 'article.html').read_text(encoding='utf-8')
refs = local_assets(html, target_d)
_, cdn_data = mapping_snapshot(target_d, refs)
for ref in {json.dumps(missing_cdn)}:
    if ref not in refs:
        raise ValueError('Upload reference outside checked set')
    p = confined_asset(target_d, ref)
    value = None
    for _try in range(4):
        try:
            value = wechat_api.upload_content_image(str(p))
            break
        except Exception as _e:
            if _try == 3:
                raise ValueError('upload failed for %s: %s' % (ref, _e))
            time.sleep(3)
    if not cdn_url(value):
        raise ValueError('Invalid uploaded CDN address')
    cdn_data[ref] = value

with open(cdn_p, 'w', encoding='utf-8') as f:
    json.dump(cdn_data, f, ensure_ascii=False, indent=2)
print("CDN_SYNC_DONE")
"""
        proc = subprocess.run(transport_args('ssh') + [SSH, remote_python()],
                              input=remote_script, check=True, capture_output=True, text=True, encoding="utf-8", timeout=120)
        # 将最新的 cdn_urls.json 回传本地
        subprocess.run(transport_args('scp') + [
                        f"{SSH}:{REMOTE_ROOT}/generated/{target_dir.name}/cdn_urls.json",
                        str(cdn_f)], check=True, capture_output=True, timeout=30)
        _, cdn_map = mapping_snapshot(frozen_target, refs)

    aliases = {}
    for source in raw_imgs:
        if not cdn_url(source):
            canonical = confined_asset(frozen_target, source).relative_to(frozen_target).as_posix()
            if canonical not in cdn_map:
                raise ValueError('图片上传后仍缺少CDN映射: %s' % canonical)
            aliases[source] = cdn_map[canonical]
    html_content = replace_sources(html_src, aliases)
    final_imgs = image_sources(html_content)
    if len(final_imgs) != len(raw_imgs) or not all(cdn_url(src) for src in final_imgs):
        raise ValueError('CDN图片数量或地址核验失败')

    (target_dir / "article_cdn.html").write_text(html_content, encoding="utf-8")

    payload = {
        "media_id": mid or "",
        "title": cj.get("title", ""),
        "author": "Jackie",
        "digest": cj.get("digest", ""),
        "content": html_content,
        # The checked local cover may have changed with the manuscript. Let
        # mp_ops upload this cover before writing; never reuse a stale ID.
        "thumb_media_id": "",
        "show_cover_pic": 1,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
        "content_source_url": "",
    }
    # 🔴 Codex 阻断项4：旧实现用固定文件名 _push_payload.json 且无操作 ID，
    #    重试时远端可能读到上一次残留的 payload，也无法判断是否已执行过。
    #    改为「每次推送一个唯一 run 目录」，并在 payload 内带 operation_id。
    payload["operation_id"] = run_id
    payload["target_slug"] = target_dir.name
    payload["mode"] = args.mode
    if args.media_id:
        payload["media_id"] = args.media_id

    run_dir = target_dir / ".studio" / "push_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    lp = run_dir / "payload.json"
    lp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    remote_run_dir = "%s/generated/%s/.studio/push_runs/%s" % (
        REMOTE_ROOT, target_dir.name, run_id)
    remote_lp = remote_run_dir + "/payload.json"

    # 同步封面与 payload 到服务器（确保远端目录存在并具备封面）
    remote_article_dir = f"{REMOTE_ROOT}/generated/{target_dir.name}"
    py_mkdir = f"import os; os.makedirs('{remote_run_dir}', exist_ok=True); os.makedirs('{remote_article_dir}', exist_ok=True)"
    subprocess.run(transport_args('ssh') + [SSH, f"{remote_python()} -c \"{py_mkdir}\""],
                   check=True, capture_output=True, timeout=30)
    cover_file = frozen_target / "cover_900x383.jpg"
    if cover_file.exists():
        time.sleep(0.3)
        subprocess.run(transport_args('scp') + [str(cover_file),
                        f"{SSH}:{REMOTE_ROOT}/generated/{target_dir.name}/"],
                       check=True, capture_output=True, timeout=30)
    time.sleep(0.3)
    subprocess.run(transport_args('scp') + [str(lp),
                    f"{SSH}:{remote_run_dir}/"],
                   check=True, capture_output=True, timeout=30)

    time.sleep(0.5)
    if (_sha256_bytes((target_dir / 'content.json').read_bytes()) != checked_content_sha
            or delivery_bundle_sha256(target_dir) != checked_bundle_sha):
        raise ValueError('授权后交付包发生变化，保留旧批准并停止写入')
    _sync_writing_review(frozen_target)

    data, raw, rc = remote_ops("push %s %s" % (remote_lp, args.mode), timeout=300,
                               write_reason=write_reason)
    if not data or not data.get("ok"):
        # R11：失败不能只报"失败"。远端可能已 add_draft 成功但回执丢了，
        # 盲目重试会建出孤儿稿。这里先做只读归类，再给明确结论。
        print("❌ push 失败：%s" % (raw or "")[:400])
        _classify_push_outcome(target_dir, run_dir, payload)
        sys.exit(1)
    # 🔴 Codex 第三轮 B01：旧实现只在「回执带 operation_id 且不同」时才拒绝，
    #    缺 operation_id 的回执照样通过。现在改为必填。
    if not data.get("operation_id"):
        print("❌ 致命错误：远端回执缺少 operation_id，无法确认本次操作归属，拒绝采信")
        _classify_push_outcome(target_dir, run_dir, payload)
        sys.exit(1)
    if data["operation_id"] != run_id:
        print("❌ 致命错误：远端回执 operation_id=%r 与本次 %r 不符，拒绝采信"
              % (data["operation_id"], run_id))
        _classify_push_outcome(target_dir, run_dir, payload)
        sys.exit(1)
    if not data.get("has_cover") or not data.get("thumb_media_id"):
        print("❌ 致命错误：推送后检测到封面缺失或 thumb_media_id 为空！")
        _classify_push_outcome(target_dir, run_dir, payload)
        sys.exit(1)
    print("  ✅ 已推送 | media_id=%s" % data["media_id"])
    print("     标题=%s" % data["title"][:50])
    print("     封面=%s (thumb_id: %s) | 汉字=%d"
          % ("有" if data["has_cover"] else "无 ❌", data.get("thumb_media_id", "")[:12], data["cn_count"]))

    # 4. 更新台账
    # 🔴 Codex JOS-036：旧实现整体替换条目，会丢掉原有字段（如 opening_variant），
    #    且从不写入变体历史，导致 Gate16「变体轮换」永远读不到记录。
    #    现在改为「保留旧字段 + 覆盖本次指纹 + 补写 opening_variant」。
    meta = load_sync_meta()
    items = meta.setdefault("items", {})
    old_entry = dict(items.get(target_dir.name) or {})
    _cj = {}
    try:
        _cj = json.loads((target_dir / "content.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    new_entry = {
        **old_entry,                       # 先保留历史字段
        "media_id": data["media_id"],
        "title": data["title"],
        "title_sha256": _sha(data["title"]),
        "content_sha256": data["content_sha256"],
        "cn_count": data["cn_count"],
        "thumb_media_id": data.get("thumb_media_id", (meta_item or {}).get("thumb_media_id", "")),
        "has_cover": data["has_cover"],
        "opening_variant": _cj.get("opening_variant") or old_entry.get("opening_variant", ""),
        "pushed_at": __import__('datetime').datetime.now(
            __import__('datetime').timezone.utc).isoformat(),
    }
    items[target_dir.name] = new_entry
    meta["policy"] = "local_first"
    SYNC_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print("  ✅ 台账已更新")

    # 5. 自动 verify
    # 🔴 Codex B03：必须把本次不可变 payload 的远端路径显式传给 verify，
    #    否则它会去读旧文件或根本读不到。
    print("\n[*] 自动执行 verify ...")
    class _A:
        media_id = data["media_id"]
        payload_path = remote_lp
    cmd_verify(_A())


def _classify_push_outcome(target_dir, run_dir, payload):
    """push 失败时的只读归类：给出能否重试的明确结论。

    绝不在此处重试、改台账或重建指纹；归类本身失败也不得掩盖原始错误。
    """
    try:
        from scripts.ops.push_recovery import classify, read_operation_state
        operation = read_operation_state(target_dir, payload.get('operation_id', ''))
        # 官方草稿列表：只读拉取；拿不到就保持 unknown，不当作未提交。
        online = None
        try:
            data, _raw, _rc = remote_ops("list")
            if data and data.get('ok'):
                online = data.get('items') or []
        except Exception:
            online = None
        result = classify(payload, operation, online)
        state = result.get('state', '')
        print("\n=== 本次写入状态归类（只读）===")
        print("  状态: %s" % state)
        print("  说明: %s" % result.get('reason', ''))
        print("  官方列表是否已查: %s" % ("是" if online is not None else "否"))
        if state == 'not_submitted_safe_to_retry':
            print("  ✅ 可安全重试：无本地记录且官方列表无同指纹草稿")
        elif state == 'failed_clean_safe_to_retry':
            print("  ✅ 可安全重试：远端明确失败且平台无同指纹草稿")
        elif state == 'submitted_unconfirmed_do_not_retry':
            print("  ⛔ 禁止重试：平台已有同指纹草稿（media_id=%s）"
                  % result.get('media_id', '?'))
            print("     请人工确认该草稿并补台账，不要新建第二篇")
        else:
            print("  ⚠️ 状态未明：先回读官方草稿箱再决定，禁止盲目重试")
        print("==============================\n")
        return result
    except Exception as exc:
        # 归类失败不能盖掉原本的 push 失败原因
        print("\n⚠️ 状态归类未完成（%s: %s），按状态未明处理，禁止盲目重试"
              % (type(exc).__name__, str(exc)[:80]))
        return None


def cmd_release(args):
    """一键发布流水线：check → diff → push → verify → audit。

    这是批次 3-5 规定的**唯一推荐发布入口**，串起所有关卡。
    任一环节失败即中止，绝不带病上线。
    """
    print("\n" + "█" * 62)
    print("🚀 release 一键发布流水线")
    print("█" * 62)
    steps = []

    # ⓿ 双向漂移检测（先确认本地与线上无未同步改动）
    print("\n【0/5】双向漂移检测 diff --both")
    try:
        import subprocess as _sp0
        _s = ROOT_DIR / "scripts" / "ops" / "bidirectional_diff.py"
        if _s.exists():
            _sp0.run([sys.executable, str(_s), '--allow-local-ahead'], timeout=600, check=True)
    except Exception as e:
        raise RuntimeError("Bidirectional diff failed; release stopped") from e

    # ① check
    print("\n【1/5】前置门禁 check")
    args.mode = getattr(args, "mode", "update")
    if not cmd_check(args):
        print("\n❌ release 中止：门禁未通过")
        sys.exit(1)
    steps.append("check ✅")

    # ② push（内部含 diff + verify）
    print("\n【2/5】本地覆盖线上 push")
    cmd_push(args)
    steps.append("push ✅")

    # ②b 强制核对本次官方回读回执
    # R11（P2）：旧 release 只打印「push ✅」，从不检查 get_draft 断言是否落盘。
    # 保存成功、返回 media_id 都不等于 draft_verified；没有本次回执就停止。
    _rdir = Path(args.dir).resolve()
    _rmeta = _rdir / "draft_meta.json"
    if not _rmeta.is_file():
        print("\n❌ release 中止：缺少 draft_meta.json，官方回读回执未落盘")
        print("   请先完成本次 verify，不新建重试操作。")
        sys.exit(4)
    _rdata = json.loads(_rmeta.read_text(encoding="utf-8"))
    if _rdata.get("draft_verified") is not True:
        print("\n❌ release 中止：draft_verified 未置真，官方回读未通过")
        sys.exit(4)
    if not str(_rdata.get("thumb_media_id") or "").strip():
        print("\n❌ release 中止：回执中 thumb_media_id 为空，封面未绑定")
        sys.exit(4)
    print("  ✅ 官方回读回执核对通过（draft_verified / 封面 / media_id）")
    steps.append("verify ✅")

    # ③ audit
    print("\n【3/5】全库风控体检 audit")
    class _V:
        verbose = False
    try:
        cmd_audit(_V())
    except RuntimeError as exc:
        print("\n❌ release 中止：audit 未通过 —— %s" % exc)
        print("   已完成 push，请立即处理阻断项后再对外发布。")
        sys.exit(5)
    steps.append("audit ✅")

    print("\n" + "█" * 62)
    print("🎉 release 完成：" + " → ".join(steps))
    print("█" * 62)


def cmd_prep(args):
    """Build a reviewable Stage 2 package without renewing approvals."""
    from scripts.sync_hash_chain import sync_article_hash_chain
    target_dir = Path(args.dir).resolve()
    report = sync_article_hash_chain(target_dir, track=args.track.upper(),
                                    track_type=args.track_type, authorized=False)
    structural_ok = cmd_check(args)
    blockers = list(report.get('production', {}).get('sample', {}).get('errors', []))
    blockers.extend(report.get('approval_record_errors', []))
    for stage, status in report.get('approvals', {}).items():
        if status.get('state') == 'invalid':
            blockers.extend(status.get('errors', [stage + ' approval invalid']))
    blockers.extend(report.get('editorial_errors', []))
    # Writing review includes delivery-stage approval checks; show it separately
    # here. Strict delivery_gate revalidates all dependencies at release.
    report['structural_passed'] = bool(structural_ok)
    report['prep_passed'] = bool(structural_ok and report.get('derived_passed') and not blockers)
    report['prep_blockers'] = blockers
    report['delivery_ready'] = not report.get('production', {}).get('delivery', {}).get('errors', []) and not report.get('writing_errors') and not blockers
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['prep_passed']:
        raise ValueError('prep 阻断：' + '; '.join(blockers or ['结构检查未通过']))
    if not report['delivery_ready']:
        print('本地全景包已准备；全文认可及写作复核仍须完成。prep 不产生草稿写入许可。')
    else:
        print('本地审核包已准备；release 仍将重验严格门禁与单次写入许可。')
    return report


def cmd_authorize(args):
    """Record explicit draft-write instruction bound to exact local bytes."""
    from scripts.write_guard import issue_write_authorization
    auth = issue_write_authorization(Path(args.dir).resolve(), args.quote,
                                     args.mode, getattr(args, 'media_id', '') or '')
    print('单次凭据已保存：当前交付包、操作模式及目标草稿绑定，30分钟有效。')
    print('稿件：%s / 模式：%s' % (auth['slug'], auth['mode']))
    return auth


# -------------------------------------------------------------
# CLI 主入口
# -------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Jackie 统一生产调度中枢 CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # render
    p_render = subparsers.add_parser("render", help="根据 content.json 与 Jinja2 模板装配 HTML")
    p_render.add_argument("--dir", required=True, help="文章工程目录路径")
    p_render.add_argument("--track", default="A", choices=["A", "B", "C", "D", "E", "F", "S"], help="轨道代号")
    # 🔴 Codex JOS-034：统一渲染入口后，契约需要 industry（产业赛道）。
    #    老稿件普遍缺该字段，必须允许从 CLI 显式提供，否则只能报错。
    p_render.add_argument("--track-type", default="fishery",
                          choices=["fishery", "planting", "livestock", "agri-tech"],
                          help="产业赛道（content.json 缺 industry 时用它补全）")
    p_render.add_argument("--force", action="store_true",
                          help="强制覆盖更长的现有 article.html（默认拒绝）")

    # check
    p_check = subparsers.add_parser("check", help="刚性前置质检门禁")
    p_check.add_argument("--dir", required=True, help="文章工程目录路径")
    p_check.add_argument("--track", default="A", choices=["A", "B", "C", "D", "E", "F", "S"], help="轨道代号")
    p_check.add_argument("--track-type", default="fishery", choices=["fishery", "planting", "livestock", "agri-tech"], help="产业赛道")

    # deploy
    p_deploy = subparsers.add_parser("deploy", help="直连腾讯云入库草稿箱")
    p_deploy.add_argument("--dir", required=True, help="文章工程目录路径")
    p_deploy.add_argument("--track", default="A", choices=["A", "B", "C", "D", "E", "F", "S"], help="轨道代号")
    p_deploy.add_argument("--track-type", default="fishery", choices=["fishery", "planting", "livestock", "agri-tech"], help="产业赛道")
    p_deploy.add_argument("--mode", default="new", choices=["new", "update"], help="new=新建草稿, update=原位更新")
    p_deploy.add_argument("--media-id", default="", help="原位更新时指定的目标草稿 media_id")

    # remote-deploy (云端执行入口)
    # 🔴 Codex 阻断项3：此入口此前不做任何门禁就直接 add_draft，是完整旁路。
    #    现改为「必须携带上游门禁回执」，没有回执一律拒绝。
    p_rem = subparsers.add_parser("remote-deploy",
                                  help="云端执行草稿落库（需上游门禁回执）")
    p_rem.add_argument("--dir", required=True, help="文章工程目录路径")
    p_rem.add_argument("--track-type", default="fishery", help="产业赛道")
    p_rem.add_argument("--track", default="A", help="内容轨道（门禁需要）")
    p_rem.add_argument("--mode", default="new", choices=["new", "update"], help="new=新建草稿, update=原位更新")
    p_rem.add_argument("--media-id", default="", help="更新时指定的目标草稿 media_id")
    p_rem.add_argument("--gate-receipt", default="",
                       help="上游 check 通过后生成的回执路径（缺省则拒绝执行）")

    # edit-images
    p_edit = subparsers.add_parser("edit-images", help="轻量唤醒配图微调工作台")
    p_edit.add_argument("--dir", required=True, help="文章工程目录路径")

    # ---- 批次 1：交付层防漂移 ----
    p_diff = subparsers.add_parser("diff", help="漂移检测：比对本地与线上指纹")
    p_diff.add_argument("--media-id", default="", help="只检查指定草稿")
    p_diff.add_argument("--both", action="store_true",
                        help="双向比对（本地↔线上↔台账），可发现「本地改了没推」")

    p_verify = subparsers.add_parser("verify", help="独立回读断言（部署后必跑）")
    p_verify.add_argument("--media-id", required=True, help="目标草稿 media_id")

    p_audit = subparsers.add_parser("audit", help="全库风控体检")
    p_audit.add_argument("-v", "--verbose", action="store_true", help="输出明细")

    p_push = subparsers.add_parser("push", help="本地覆盖线上（强制先 check + diff）")
    p_push.add_argument("--dir", required=True, help="文章工程目录路径")
    p_push.add_argument("--track", default="A", choices=["A", "B", "C", "D", "E", "F", "S"])
    p_push.add_argument("--track-type", default="fishery",
                        choices=["fishery", "planting", "livestock", "agri-tech"])
    p_push.add_argument("--mode", default="update", choices=["new", "update"])
    p_push.add_argument("--media-id", default="", help="目标草稿 media_id")
    p_push.add_argument("--force", action="store_true",
                        help="线上有漂移时强制覆盖")

    p_doc = subparsers.add_parser("doccheck", help="文档硬数字核对")
    p_doc.add_argument("--quiet", action="store_true")

    p_inspect = subparsers.add_parser("inspect", help="微信公众平台后台全自动数据巡检与飞轮分析")

    # ---- 外部前沿情报雷达 (AIHOT 垂直收割 + 海外一手争议) ----
    p_radar = subparsers.add_parser("radar", help="外部垂直情报雷达：AIHOT 靶向收割与海外一手争议原声")
    p_radar.add_argument("--source", choices=["domestic", "overseas", "aihot", "all"], default="all",
                         help="情报信源: domestic (国内一手热搜/实体硬件/产业) / overseas (海外HN与垂直评测) / aihot (仅国内AI) / all (全球全量)")
    p_radar.add_argument("--track", choices=["all", "fishery", "planting", "livestock", "agri-tech", "physical-ai"], default="all",
                         help="指定扫描赛道 (默认 all)")
    p_radar.add_argument("--window", choices=["24h", "7d"], default="7d",
                         help="时间窗口 (默认 7d)")
    p_radar.add_argument("--keywords", type=str, default=None,
                         help="自定义垂直关键词逗号分隔")
    p_radar.add_argument("--limit", type=int, default=10,
                         help="单词条目上限 (默认 10)")
    p_radar.add_argument("--promote", action="store_true",
                         help="自动将评分≥8.5的高潜选题注入待做选题库")

    p_rel = subparsers.add_parser("release",
                                  help="一键发布：check → push → verify → audit")
    p_rel.add_argument("--dir", required=True, help="文章工程目录路径")
    p_rel.add_argument("--track", default="A", choices=["A", "B", "C", "D", "E", "F", "S"])
    p_rel.add_argument("--track-type", default="fishery",
                       choices=["fishery", "planting", "livestock", "agri-tech"])
    p_rel.add_argument("--mode", default="update", choices=["new", "update"])
    p_rel.add_argument("--media-id", default="", help="目标草稿 media_id")
    p_rel.add_argument("--force", action="store_true", help="线上漂移时强制覆盖")

    p_prep = subparsers.add_parser("prep",
                                   help="重建全景审阅包，只读核对证据；不续签批准")
    p_prep.add_argument("--dir", required=True, help="文章工程目录路径")
    p_prep.add_argument("--track", default="A", choices=["A", "B", "C", "D", "E", "F", "S"])
    p_prep.add_argument("--track-type", default="fishery",
                        choices=["fishery", "planting", "livestock", "agri-tech"])
    p_prep.add_argument("--force", action="store_true", help="强制覆盖已有渲染")

    p_auth = subparsers.add_parser(
        "authorize",
        help="生成一次性草稿写入授权（绑定 slug + content SHA，30 分钟，消费即删）")
    p_auth.add_argument("--dir", required=True, help="文章工程目录路径")
    p_auth.add_argument("--quote", required=True, help="用户本轮真实授权原话")
    p_auth.add_argument("--mode", required=True, choices=["new", "update"])
    p_auth.add_argument("--media-id", default="", help="update模式绑定的目标草稿ID")

    args = parser.parse_args()

    try:
        _dispatch(args)
    except (RuntimeError, ValueError) as exc:
        # 业务函数以 RuntimeError/ValueError 表达阻断；CLI 顶层统一转为非零退出，不抛栈。
        print("\n❌ %s" % exc)
        sys.exit(1)


def _dispatch(args):
    if args.command == "render":
        cmd_render(args)
    elif args.command == "check":
        ok = cmd_check(args)
        sys.exit(0 if ok else 1)
    elif args.command == "deploy":
        cmd_deploy(args)
    elif args.command == "remote-deploy":
        cmd_remote_deploy(args)
    elif args.command == "edit-images":
        cmd_edit_images(args)
    elif args.command == "diff":
        cmd_diff(args)
    elif args.command == "verify":
        cmd_verify(args)
    elif args.command == "audit":
        cmd_audit(args)
    elif args.command == "push":
        cmd_push(args)
    elif args.command == "release":
        cmd_release(args)
    elif args.command == "prep":
        cmd_prep(args)
    elif args.command == "authorize":
        cmd_authorize(args)
    elif args.command == "inspect":
        import subprocess as _sp
        inspector_script = ROOT_DIR / ".agents" / "skills" / "wechat-data-inspector" / "scripts" / "inspect_backend.py"
        _p = _sp.run([sys.executable, str(inspector_script)])
        sys.exit(_p.returncode)
    elif args.command == "radar":
        import subprocess as _sp
        unified_script = ROOT_DIR / "scripts" / "radar" / "unified_radar.py"
        # 参数必须真正透传，否则 CLI 声明等于装饰（JOS-037）。
        cmd = [sys.executable, "-X", "utf8", str(unified_script), "--source", args.source]
        if getattr(args, "track", None):
            cmd += ["--track", args.track]
        if getattr(args, "window", None):
            cmd += ["--window", args.window]
        if getattr(args, "keywords", None):
            cmd += ["--keywords", args.keywords]
        if getattr(args, "limit", None) is not None:
            cmd += ["--limit", str(args.limit)]
        if args.promote:
            cmd.append("--promote")
        print("[*] radar 实际生效参数: %s" % " ".join(cmd[3:]))
        _p = _sp.run(cmd)
        sys.exit(_p.returncode)
    elif args.command == "doccheck":
        import subprocess as _sp
        _p = _sp.run([sys.executable, str(ROOT_DIR / "scripts" / "doccheck.py")])
        sys.exit(_p.returncode)

if __name__ == "__main__":
    main()
