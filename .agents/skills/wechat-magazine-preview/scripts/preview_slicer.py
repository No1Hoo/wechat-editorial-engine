#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
wechat-magazine-preview: 微信商业杂志级真机视口切片与 Generative UI 预览工坊
自动化完成：
1. 启动无头浏览器（Playwright），按移动端 1:1 视口渲染 `preview.html` 并导出高清长图；
2. 按 6 屏叙事结构（导语首屏/矛盾撕裂/硬账标尺/工业现场/商业博弈/纯净结语）智能切出带轻微重叠的高清切片；
3. 自动生成内嵌自适应手机外壳、灵动岛、平滑滚屏监听与标签页导航的 `preview_widget.html`。
"""

import argparse
import asyncio
import base64
import io
import json
import os
import sys
from pathlib import Path
from PIL import Image

VIEWPORT_WIDTH = 480
VIEWPORT_HEIGHT = 800
DEVICE_SCALE = 2
OVERLAP_PX = 150

DEFAULT_SLICES_MAP = [
    "1. 导语与首屏实景",
    "2. 核心价格与矛盾撕裂",
    "3. 单位经济与硬账标尺",
    "4. 工业装备与微观工艺",
    "5. 企业博弈与创新突破",
    "6. 产业护城河与纯净结语"
]

async def capture_full_screenshot(html_path: Path, output_png: Path):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        context = await browser.new_context(
            viewport={'width': VIEWPORT_WIDTH, 'height': VIEWPORT_HEIGHT},
            device_scale_factor=DEVICE_SCALE
        )
        page = await context.new_page()
        file_uri = html_path.resolve().as_uri()
        await page.goto(file_uri)
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(output_png), full_page=True)
        await browser.close()
    print(f"[*] 成功截取真机高清长图: {output_png} (文件大小: {output_png.stat().st_size // 1024} KB)")

def slice_screenshot(full_png: Path, out_dir: Path, prefix: str, slice_count: int = 6):
    im = Image.open(full_png)
    w, h = im.size
    print(f"[*] 长图规格: {w} × {h} px")
    
    slice_h = h // slice_count
    slice_files = []
    
    for i in range(slice_count):
        top = max(0, i * slice_h - (OVERLAP_PX if i > 0 else 0))
        bottom = min(h, (i + 1) * slice_h + (OVERLAP_PX if i < (slice_count - 1) else 0))
        box = (0, top, w, bottom)
        slice_im = im.crop(box)
        
        slice_name = f"{prefix}_{i+1}_slice.png"
        slice_path = out_dir / slice_name
        slice_im.save(slice_path, format="PNG")
        slice_files.append((i, slice_name, slice_path, w, bottom - top))
        print(f"  - 切片 {i+1}: {slice_name} ({w} × {bottom-top} px)")
        
    return slice_files

def generate_widget_html(slice_files, title: str, subtitle: str, out_html: Path):
    slice_b64 = []
    for idx, name, path, w, h in slice_files:
        im = Image.open(path)
        im_small = im.resize((im.width // 2, im.height // 2), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im_small.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        
        nav_label = DEFAULT_SLICES_MAP[idx] if idx < len(DEFAULT_SLICES_MAP) else f"第 {idx+1} 屏"
        slice_b64.append({
            "idx": idx,
            "title": nav_label,
            "data": f"data:image/jpeg;base64,{b64}"
        })

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} · 真机视口预览</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #0f172a; color: #f8fafc; display: flex; flex-direction: column; align-items: center; min-height: 100vh; padding: 20px 10px; }}
  .header {{ text-align: center; max-width: 600px; margin-bottom: 20px; }}
  .header h1 {{ font-size: 20px; font-weight: 700; color: #38bdf8; margin-bottom: 8px; line-height: 1.4; }}
  .header p {{ font-size: 13px; color: #94a3b8; line-height: 1.5; }}
  .badge-tag {{ display: inline-block; background: #0369a1; color: #e0f2fe; font-size: 11px; padding: 2px 8px; border-radius: 4px; margin-bottom: 8px; font-weight: 500; }}
  .phone-mockup {{ width: 100%; max-width: 420px; background: #1e293b; border-radius: 36px; border: 8px solid #334155; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.7); overflow: hidden; display: flex; flex-direction: column; }}
  .phone-notch {{ height: 24px; background: #334155; display: flex; justify-content: center; align-items: center; }}
  .phone-notch-bar {{ width: 90px; height: 5px; background: #1e293b; border-radius: 3px; }}
  .phone-screen {{ height: 680px; overflow-y: auto; scroll-behavior: smooth; position: relative; background: #fff; }}
  .nav-bar {{ display: flex; overflow-x: auto; gap: 8px; padding: 12px; background: #1e293b; border-top: 1px solid #334155; scrollbar-width: none; }}
  .nav-bar::-webkit-scrollbar {{ display: none; }}
  .nav-btn {{ flex: 0 0 auto; background: #334155; border: none; color: #cbd5e1; font-size: 12px; padding: 6px 12px; border-radius: 16px; cursor: pointer; transition: all 0.2s; }}
  .nav-btn.active {{ background: #38bdf8; color: #0f172a; font-weight: 600; }}
  .slice-container {{ width: 100%; display: block; }}
  .slice-container img {{ width: 100%; height: auto; display: block; }}
</style>
</head>
<body>

<div class="header">
  <span class="badge-tag">商业杂志级真机视口 · Generative UI</span>
  <h1>{title}</h1>
  <p>{subtitle}</p>
</div>

<div class="phone-mockup">
  <div class="phone-notch"><div class="phone-notch-bar"></div></div>
  <div class="phone-screen" id="phoneScreen">
    {"".join(f'<div class="slice-container" id="slice-{s["idx"]}"><img src="{s["data"]}" alt="{s["title"]}"></div>' for s in slice_b64)}
  </div>
  <div class="nav-bar">
    {"".join(f'<button class="nav-btn {"active" if idx == 0 else ""}" onclick="scrollToSlice({idx})">{s["title"].split(". ")[1] if ". " in s["title"] else s["title"]}</button>' for idx, s in enumerate(slice_b64))}
  </div>
</div>

<script>
const screen = document.getElementById('phoneScreen');
const navBtns = document.querySelectorAll('.nav-btn');

function scrollToSlice(idx) {{
  const target = document.getElementById('slice-' + idx);
  if (target) {{
    screen.scrollTo({{
      top: target.offsetTop,
      behavior: 'smooth'
    }});
    navBtns.forEach((b, i) => b.classList.toggle('active', i === idx));
  }}
}}

screen.addEventListener('scroll', () => {{
  const scrollPos = screen.scrollTop + 100;
  for (let i = 0; i < {len(slice_b64)}; i++) {{
    const el = document.getElementById('slice-' + i);
    if (el && scrollPos >= el.offsetTop && scrollPos < el.offsetTop + el.offsetHeight) {{
      navBtns.forEach((b, idx) => b.classList.toggle('active', idx === i));
      break;
    }}
  }}
}});
</script>

</body>
</html>
"""
    with open(out_html, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print(f"[*] 成功构建交互式真机预览小部件: {out_html} (大小: {out_html.stat().st_size // 1024} KB)")

def main():
    parser = argparse.ArgumentParser(description="微信商业杂志级真机视口切片与 Generative UI 预览工坊")
    parser.add_argument("--dir", required=True, help="文章工程目录路径（如 generated/2026-10-30-yuhang-jingshan-matcha-supply-chain）")
    parser.add_argument("--slices", type=int, default=6, help="视口切片数量（默认 6 屏）")
    parser.add_argument("--prefix", default="", help="切片文件名前缀（默认取文章目录名简写）")
    parser.add_argument("--art-dir", default=r"C:\Users\26288\.gemini\antigravity\brain\c2fb8882-00f5-4998-aedf-74447f8f84d4", help="Artifact 保存目录")
    
    args = parser.parse_args()
    article_dir = Path(args.dir).resolve()
    preview_html = article_dir / "preview.html"
    
    if not preview_html.exists():
        print(f"[!] 未找到 preview.html: {preview_html}，请先运行 pipeline.py render 渲染产物！")
        sys.exit(1)
        
    content_json = article_dir / "content.json"
    title = "微信商业文章预览"
    subtitle = "真机 1:1 视口切片"
    if content_json.exists():
        try:
            c = json.loads(content_json.read_text(encoding="utf-8"))
            title = c.get("title", title)
            digest = c.get("digest", "")
            subtitle = f"纯正文 {c.get('metadata', {}).get('cn_chars', '~2600')} 字 · 8张16:9横图实景 · 零模板化定制排版"
        except Exception:
            pass

    art_dir = Path(args.art_dir).resolve()
    art_dir.mkdir(parents=True, exist_ok=True)
    
    prefix = args.prefix or article_dir.name[:20].replace("-", "_")
    full_png = art_dir / f"{prefix}_full_redesign.png"
    
    print(f"=== 🚀 开始执行移动端 1:1 真机视口渲染 ===")
    print(f"  文章目录: {article_dir}")
    print(f"  标题: {title}")
    
    asyncio.run(capture_full_screenshot(preview_html, full_png))
    slice_files = slice_screenshot(full_png, art_dir, prefix, slice_count=args.slices)
    out_widget = art_dir / f"preview_{prefix}_widget.html"
    generate_widget_html(slice_files, title, subtitle, out_widget)
    
    print("\n🎉 全部工序完成！")
    print(f"  交互式预览组件: {out_widget}")
    print(f"  视口切片清单:")
    for idx, name, p, w, h in slice_files:
        print(f"    - [{DEFAULT_SLICES_MAP[idx] if idx < len(DEFAULT_SLICES_MAP) else idx}]: {p}")

if __name__ == "__main__":
    main()
