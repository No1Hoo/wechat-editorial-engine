#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jackie 观察视角 · 单篇深度流量穿透与算法诊断引擎 (Single Article Traffic Diagnostic Engine)
专门用于：
  1. 穿透单篇图文详情页 (misc/appmsganalysis?action=detailpage)
  2. 提取真实渠道构成 (公众号消息、朋友圈、聊天、主页、搜一搜、系统推荐公域)
  3. 核验群发通知状态 (【已开启群发通知】 vs 【未开启群发通知】)，一票定性是否被静默发布误杀
  4. 提取完读率、送达阅读率、裂变扩散率、在看(haokan)与受众画像 (省份Top5、性别)
  5. 自动输出标准化诊断研报，免去繁琐的人工 DOM 探测与 Token 浪费
"""

import sys
import os
import re
import json
import time
import argparse
from datetime import datetime
from pathlib import Path

# 编码设置
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

ROOT_DIR = Path(__file__).resolve().parents[4]
if not (ROOT_DIR / "pipeline.py").exists():
    fallback = Path("D:/gemini 微信公众号创作区")
    if fallback.exists():
        ROOT_DIR = fallback

REPORTS_DIR = ROOT_DIR / "reports"
SNAPSHOTS_DIR = REPORTS_DIR / "snapshots"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# 导入底层受控 BSK 会话接口
sys.path.insert(0, str((ROOT_DIR / ".agents/skills/wechat-data-inspector/scripts").resolve()))
from inspect_backend import (
    ensure_bsk_daemon, bsk_start_session, bsk_stop_session,
    bsk_get_agent_tab, obtain_valid_token, bsk_navigate, bsk_evaluate
)

def extract_article_list(session_id, tab_id, token, front_type="all"):
    """
    从已发表内容列表页提取最近文章及详情页链接
    front_type: 'all' (已通知内容) 或 'without_notice' (未开启通知内容)
    """
    if front_type == "without_notice":
        url = f"https://mp.weixin.qq.com/misc/appmsganalysis?action=all&front_type=without_notice&type=daily_v2&token={token}&lang=zh_CN"
        notice_status = "未开启群发通知"
    else:
        url = f"https://mp.weixin.qq.com/misc/appmsganalysis?action=all&type=daily_v2&token={token}&lang=zh_CN"
        notice_status = "已开启群发通知"

    bsk_navigate(session_id, tab_id, url, wait_ms=2500)
    bsk_evaluate(session_id, tab_id, "window.scrollTo(0, 1000);")
    time.sleep(1)

    js_code = """
    (() => {
        const items = [];
        document.querySelectorAll('a').forEach(a => {
            if (a.innerText && a.innerText.trim() === '详情' && a.href && a.href.includes('detailpage')) {
                let tr = a.closest('tr');
                let parentText = tr ? tr.innerText.replace(/\\s+/g, ' ') : '';
                
                // 解析 URL 中的 msgid 与 publish_date
                const u = new URL(a.href);
                const msgid = u.searchParams.get('msgid') || '';
                const publishDate = u.searchParams.get('publish_date') || '';
                
                // 从父行文本解析标题
                let title = '';
                const titleMatch = parentText.match(/^(.*?)(?:\\s+发表时间|\\s+\\d{4}-\\d{2}-\\d{2})/);
                if (titleMatch) {
                    title = titleMatch[1].trim();
                } else {
                    title = parentText.split(' ')[0] || '';
                }

                items.push({
                    title: title,
                    msgid: msgid,
                    publish_date: publishDate,
                    detail_url: a.href,
                    row_text: parentText
                });
            }
        });
        return items;
    })()
    """
    articles = bsk_evaluate(session_id, tab_id, js_code) or []
    for art in articles:
        art['notification_type'] = notice_status
    return articles

def parse_single_article_detail(session_id, tab_id, detail_url):
    """
    深入单篇详情页，提取完整指标与渠道分布
    """
    bsk_navigate(session_id, tab_id, detail_url, wait_ms=3000)
    
    # 滚动触发展示全部图表
    bsk_evaluate(session_id, tab_id, "window.scrollTo(0, document.body.scrollHeight/2);")
    time.sleep(1)
    bsk_evaluate(session_id, tab_id, "window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1.5)

    js_code = """
    (() => {
        const text = document.body.innerText;
        const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
        
        // 1. 查找是否开启群发通知
        let noticeStatus = '未知';
        for (const l of lines) {
            if (l.includes('已开启群发通知')) {
                noticeStatus = '已开启群发通知';
                break;
            }
            if (l.includes('未开启群发通知')) {
                noticeStatus = '未开启群发通知';
                break;
            }
        }
        
        // 2. 交互项目提取（分享、点赞、在看/推荐、留言、收藏、赞赏）
        let likeUv = '0 人', haokanUv = '0 人', shareUv = '0 人', commentCnt = '0 条', favUv = '0 人', rewardAmt = '0 元';
        
        document.querySelectorAll('.weui-desktop-data-overview__item, .weui-desktop-tooltip__wrp, .weui-desktop-mass-media__data-item').forEach(el => {
            const t = el.innerText.trim();
            const tip = el.querySelector('.weui-desktop-tooltip') ? el.querySelector('.weui-desktop-tooltip').innerText.trim() : '';
            const allT = t + ' ' + tip;
            if (allT.includes('推荐') || allT.includes('在看')) {
                const m = t.match(/(\\d+)\\s*人?/);
                if (m) haokanUv = m[1] + ' 人';
            } else if (allT.includes('点赞') || allT.includes('赞')) {
                const m = t.match(/(\\d+)\\s*人?/);
                if (m) likeUv = m[1] + ' 人';
            }
        });
        
        // 3. 提取表格（地域分布等）
        const tables = Array.from(document.querySelectorAll('table')).map(t => 
            Array.from(t.querySelectorAll('tr')).map(r => 
                Array.from(r.querySelectorAll('th, td')).map(c => c.innerText.trim()).join(' | ')
            )
        );
        
        // 4. 性别与转化
        let malePct = null, femalePct = null;
        for (const l of lines) {
            if (l.includes('男') && l.includes('%')) {
                const m = l.match(/男\\s*(\\d+\\.?\\d*\\%)/);
                if (m) malePct = m[1];
            }
            if (l.includes('女') && l.includes('%')) {
                const m = l.match(/女\\s*(\\d+\\.?\\d*\\%)/);
                if (m) femalePct = m[1];
            }
        }

        return {
            title: document.title,
            noticeStatus: noticeStatus,
            lines: lines.slice(0, 160),
            tables: tables,
            haokanUv: haokanUv,
            likeUv: likeUv,
            gender: { male: malePct, female: femalePct }
        };
    })()
    """
    raw_data = bsk_evaluate(session_id, tab_id, js_code) or {}
    lines = raw_data.get('lines', [])

    # 结构化解析 lines
    metrics = {
        "notice_status": raw_data.get('noticeStatus', '未知'),
        "read_uv": "—",
        "avg_duration": "—",
        "read_completion_rate": "—",
        "new_followers": "—",
        "listen_count": "—",
        "share_uv": "—",
        "like_uv": raw_data.get('likeUv', '0 人'),
        "comment_cnt": "0 条",
        "fav_uv": "0 人",
        "haokan_uv": raw_data.get('haokanUv', '0 人'),
        "reward_amount": "0 元",
        "delivery_rate": "—",
        "first_share_rate": "—",
        "cascade_rate": "—",
        "channels": {},
        "provinces": [],
        "gender": raw_data.get('gender', {})
    }

    # 提取单行指标
    for i, line in enumerate(lines):
        if line == "阅读" and i + 1 < len(lines):
            metrics["read_uv"] = lines[i + 1]
        elif line == "平均阅读时长" and i + 1 < len(lines):
            metrics["avg_duration"] = lines[i + 1]
        elif line == "完读率" and i + 1 < len(lines):
            metrics["read_completion_rate"] = lines[i + 1]
        elif line == "新增关注" and i + 1 < len(lines):
            metrics["new_followers"] = lines[i + 1]
        elif line == "听全文" and i + 1 < len(lines):
            metrics["listen_count"] = lines[i + 1]
        elif line == "分享" and i + 1 < len(lines):
            metrics["share_uv"] = lines[i + 1]
        elif line == "留言" and i + 1 < len(lines):
            metrics["comment_cnt"] = lines[i + 1]
        elif line == "收藏" and i + 1 < len(lines):
            metrics["fav_uv"] = lines[i + 1]
        elif line == "赞赏" and i + 1 < len(lines):
            metrics["reward_amount"] = lines[i + 1]
        elif line == "订阅漏斗分析" and i + 2 < len(lines):
            metrics["delivery_rate"] = lines[i + 1]
            metrics["first_share_rate"] = lines[i + 2]
        elif line == "分享扩散分析" and i + 1 < len(lines):
            if "%" in lines[i + 1]:
                metrics["cascade_rate"] = lines[i + 1]

    # 提取渠道比例
    # 在 lines 中定位 "阅读渠道构成"
    try:
        idx = lines.index("阅读渠道构成")
        # 寻找后续的渠道名和百分比
        # 微信 DOM 常将渠道名放一簇，百分比放一簇
        found_pcts = []
        found_names = []
        for j in range(idx + 1, min(idx + 35, len(lines))):
            item = lines[j]
            if re.match(r'^\d+\.\d+\%$', item):
                found_pcts.append(item)
            elif item in ['公众号消息', '朋友圈', '聊天会话', '公众号主页', '其它', '搜一搜', '推荐']:
                if item not in found_names:
                    found_names.append(item)
        
        # 对应配对
        if len(found_names) == len(found_pcts) and len(found_names) > 0:
            for name, pct in zip(found_names, found_pcts):
                metrics["channels"][name] = pct
        elif len(found_pcts) > 0:
            # 如果顺序一致
            standard_order = ['公众号消息', '朋友圈', '聊天会话', '公众号主页', '其它', '搜一搜', '推荐']
            for k, pct in enumerate(found_pcts[:len(standard_order)]):
                channel_name = standard_order[k] if k < len(standard_order) else f"渠道{k+1}"
                metrics["channels"][channel_name] = pct
    except Exception:
        pass

    # 提取地域分布
    tables = raw_data.get('tables', [])
    for tbl in tables:
        for row_str in tbl:
            if "占比" in row_str or "地域" in row_str:
                continue
            parts = [p.strip() for p in row_str.split('|')]
            if len(parts) >= 2 and "%" in parts[1]:
                metrics["provinces"].append({
                    "province": parts[0],
                    "pct": parts[1]
                })

    return metrics

def run_diagnostics(limit=3, msgid=None):
    """
    主巡检流程
    """
    if not ensure_bsk_daemon():
        print("[-] 启动 BSK 守护进程失败！")
        return None

    session_id = bsk_start_session()
    try:
        tab_id = bsk_get_agent_tab(session_id)
        token = obtain_valid_token(session_id, tab_id)
        
        print(f"[*] 成功连接微信公众平台后台 (token={token})")
        print("[*] 正在扫描已发表图文列表（已通知 + 未通知）...")
        
        # 1. 抓取已通知文章
        notified_articles = extract_article_list(session_id, tab_id, token, front_type="all")
        # 2. 抓取未开启通知文章
        unnotified_articles = extract_article_list(session_id, tab_id, token, front_type="without_notice")
        
        all_articles = notified_articles + unnotified_articles
        print(f"[+] 发现已通知文章 {len(notified_articles)} 篇，未开启通知文章 {len(unnotified_articles)} 篇")
        
        # 筛选需要诊断的目标
        target_articles = []
        if msgid:
            target_articles = [a for a in all_articles if msgid in a['msgid']]
        else:
            target_articles = all_articles[:limit]
            
        if not target_articles:
            print("[-] 未找到待诊断的目标文章！")
            return None

        diagnostic_results = []
        for art in target_articles:
            print(f"[*] 深入穿透详情页: 《{art['title']}》 ({art['publish_date']})")
            detail = parse_single_article_detail(session_id, tab_id, art['detail_url'])
            detail['title'] = art['title']
            detail['msgid'] = art['msgid']
            detail['publish_date'] = art['publish_date']
            detail['list_notification_type'] = art['notification_type']
            diagnostic_results.append(detail)

        # 生成研报
        now = datetime.now()
        report_md = f"""# Jackie 观察视角 · 单篇深度流量诊断研报 ({now.strftime('%Y-%m-%d %H:%M')})

> **唯一事实源**：微信公众平台单篇深度分析（`appmsganalysis?action=detailpage`）
> **诊断时间**：{now.isoformat()}

---

## 一、 最近发表文章深度指标对比台账

| 指标维度 | """ + " | ".join([f"《{d['title'][:18]}...》" if len(d['title']) > 18 else f"《{d['title']}》" for d in diagnostic_results]) + " |\n"

        report_md += "| :--- | " + " | ".join([":---:" for _ in diagnostic_results]) + " |\n"
        report_md += "| **群发通知状态** | " + " | ".join([f"**{d['notice_status']}**" if '已' in d['notice_status'] else f"🔴 **{d['notice_status']}**" for d in diagnostic_results]) + " |\n"
        report_md += "| **阅读人数 (UV)** | " + " | ".join([f"**{d['read_uv']}**" for d in diagnostic_results]) + " |\n"
        report_md += "| **完读率** | " + " | ".join([f"{d['read_completion_rate']}" for d in diagnostic_results]) + " |\n"
        report_md += "| **平均停留时长** | " + " | ".join([f"{d['avg_duration']}" for d in diagnostic_results]) + " |\n"
        report_md += "| **送达阅读率** | " + " | ".join([f"{d['delivery_rate']}" for d in diagnostic_results]) + " |\n"
        report_md += "| **分享人数** | " + " | ".join([f"**{d['share_uv']}**" for d in diagnostic_results]) + " |\n"
        report_md += "| **分享扩散率** | " + " | ".join([f"{d['cascade_rate']}" for d in diagnostic_results]) + " |\n"
        report_md += "| **推荐 (在看)** | " + " | ".join([f"**{d['haokan_uv']}**" for d in diagnostic_results]) + " |\n"
        report_md += "| **新增关注** | " + " | ".join([f"{d['new_followers']}" for d in diagnostic_results]) + " |\n"

        report_md += "\n---\n\n## 二、 阅读渠道构成精确百分比（公域推荐 vs 私域社交）\n\n"
        report_md += "| 渠道来源 | " + " | ".join([f"《{d['title'][:15]}...》" for d in diagnostic_results]) + " |\n"
        report_md += "| :--- | " + " | ".join([":---:" for _ in diagnostic_results]) + " |\n"
        
        channels = ['公众号消息', '朋友圈', '聊天会话', '公众号主页', '其它', '搜一搜', '推荐']
        for ch in channels:
            ch_name = f"🔴 **{ch} (公域流)**" if ch == '推荐' else (f"🟢 **{ch}**" if ch in ['公众号消息', '朋友圈'] else ch)
            report_md += f"| {ch_name} | " + " | ".join([d['channels'].get(ch, '—') for d in diagnostic_results]) + " |\n"

        report_md += "\n---\n\n## 三、 算法与运营硬核诊断结论\n\n"
        for d in diagnostic_results:
            report_md += f"### 诊断：《{d['title']}》\n"
            if d['notice_status'] == '未开启群发通知':
                report_md += "- 🔴 **核心硬伤（操作误杀）**：发布时为【未开启群发通知】模式，粉丝未收到推送通知，初始样本池过小导致无法触发公域推荐。\n"
            else:
                report_md += "- 🟢 **群发通知**：已正常推送到粉丝微信消息列表。\n"
            
            rec_pct = d['channels'].get('推荐', '0.0%')
            if rec_pct == '0.0%' or rec_pct == '—':
                report_md += "- ⚠️ **公域推荐流未启动原因**：\n"
                report_md += f"  1. 完读率为 {d['read_completion_rate']}，送达阅读率为 {d['delivery_rate']}；\n"
                report_md += f"  2. 【推荐/在看】为 {d['haokan_uv']}（公域社交扳机未触发）；\n"
                report_md += f"  3. 流量主要依靠私域消息与朋友圈扩散支撑，处于 D+1 观察沉淀周期。\n"
            else:
                report_md += f"- 🚀 **公域推荐流已放量**：算法推荐占比已达 {rec_pct}！\n"

        # 保存研报与快照
        report_file = REPORTS_DIR / f"ARTICLE_DIAGNOSIS_{now.strftime('%Y%m%d_%H%M%S')}.md"
        report_file.write_text(report_md, encoding='utf-8')
        
        snapshot_file = SNAPSHOTS_DIR / f"article_diagnosis_{now.strftime('%Y%m%d_%H%M%S')}.json"
        snapshot_file.write_text(json.dumps(diagnostic_results, ensure_ascii=False, indent=2), encoding='utf-8')
        
        print(f"[+] 诊断研报已生成: {report_file}")
        print(f"[+] 诊断快照已落盘: {snapshot_file}")
        
        # 打印到控制台供快速查阅
        print("\n" + "=" * 60)
        print(report_md)
        print("=" * 60)
        
        return diagnostic_results

    finally:
        bsk_stop_session(session_id)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Jackie 观察视角 · 单篇深度流量穿透诊断")
    parser.add_argument('--limit', type=int, default=3, help="诊断最近发布的文章篇数（默认3篇）")
    parser.add_argument('--msgid', type=str, default=None, help="指定诊断特定文章 msgid")
    args = parser.parse_args()
    
    run_diagnostics(limit=args.limit, msgid=args.msgid)
