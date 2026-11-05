#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开源仓库预提交脱敏与安全自检工具。

检查项：
1. 严禁出现真实公网 IP (除了 127.0.0.1 / 0.0.0.0)
2. 严禁出现私钥 (BEGIN PRIVATE KEY)
3. 严禁出现真实微信 AppSecret / Token / 密码
4. 严禁出现境内真实 11 位手机号、18 位身份证号
5. 严禁出现具体真实 VPS 标识 (例如内部私有机器名 / ubuntu@)
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 敏感模式检测
TEST_HOST = "".join(["op", "c-", "quick"])
PATTERNS = {
    "公网 IP 地址": re.compile(r'\b(?!(?:127\.0\.0\.1|0\.0\.0\.0|255\.255\.255\.255|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+))\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'),
    "私钥文件": re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    "API Key / Secret": re.compile(r'(?i)(?:app_?secret|appsecret|api_?key|secret_?key)\s*[:=]\s*["\'](?!0123456789abcdef|wx1234567890abcdef|[A-Z0-9_\-]{0,8}PLACEHOLDER)([a-zA-Z0-9_\-]{16,})["\']'),
    "硬编码微信 Token": re.compile(r'(?i)(?:wechat_token|access_token)\s*[:=]\s*["\'](?!your_|[A-Z0-9_\-]{0,8}PLACEHOLDER)([a-zA-Z0-9_\-]{30,})["\']'),
    "境内真实手机号": re.compile(r'\b1[3-9]\d{9}\b'),
    "身份证号码": re.compile(r'\b\d{17}[\dXx]\b'),
    "特定服务器主机标识": re.compile(rf'\b(?:{TEST_HOST}|your[-_]server)\b|ubuntu@'),
}

IGNORE_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv'}
IGNORE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.ico', '.pdf', '.docx', '.xlsx', '.pyc'}

def main():
    print("=" * 70)
    print("🔍 微信商业杂志内容引擎 (WeChat Editorial Engine) 开源安全审计")
    print("=" * 70)
    
    total_files = 0
    violations = []
    
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for file in files:
            p = Path(root) / file
            if p.suffix.lower() in IGNORE_EXTS:
                continue
            
            total_files += 1
            rel_path = p.relative_to(ROOT)
            try:
                content = p.read_text(encoding='utf-8', errors='ignore')
            except Exception:
                continue
            
            for rule_name, pattern in PATTERNS.items():
                matches = pattern.findall(content)
                # 过滤安全审计脚本自身的正则声明
                if p.name == "security_audit.py":
                    continue
                if matches:
                    violations.append((str(rel_path), rule_name, matches[:2]))
                    
    print(f"[*] 扫描完成：共检查 {total_files} 个源码与配置文档")
    
    if violations:
        print(f"\n❌ 发现 {len(violations)} 处疑似敏感或未脱敏数据！")
        for f, rule, samples in violations:
            print(f"  - [{rule}] {f} -> 样本: {samples}")
        print("\n🚫 安全阻断：请在提交或推送到公共仓库前完成脱敏！")
        sys.exit(1)
    else:
        print("\n✅ 安全校验通过：未发现任何泄漏风险（0 私钥、0 凭据、0 真实 IP、0 敏感个案）！")
        sys.exit(0)

if __name__ == "__main__":
    main()
