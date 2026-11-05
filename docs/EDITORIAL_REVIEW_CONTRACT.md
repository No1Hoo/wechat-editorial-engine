# 正文编辑复核契约

2026-10-20，配套 JACKIE-EDITORIAL-2026-10-20-R2。适用新稿写入及旧稿实际更新，不自动改变历史批准。

写作自查不是用户审稿。实际写入同时执行[生产阶段契约](PRODUCTION_REVIEW_CONTRACT.md)，先留真实用户对具体选题、试写及当前全文的认可，再进入本契约的技术绑定；暂停表优先。AI不能自行签发用户认可。

## 制作与复核

先读 `references/editorial_policy.json`，按本篇证据回答具体问题。写作不是填满固定栏目；选择叙事顺序、详略和结尾时，说明材料为什么需要这样组织。真实数据、采访、照片和作者判断各保留边界。不通过换词掩盖旧结构，也不按词频把正常工程解释判为不合格。

完成正文后，实际逐段阅读并留存 `evidence/writing_review.md`：记录本篇新增判断、主要论据与局限，哪些段落删改，结构为何适合本篇，哪些修辞空泛，结尾是否超出证据；有问题保留为阻断。不能只写“已检查、通过”。AI复核如实声明，不代表人类或法务批准。

再建立 `writing_review.json`。下面是字段说明，不是可以直接当成批准的成品：

```json
{
  "schema": 1,
  "policy_sha256": "当前政策文件原始字节SHA256",
  "content_sha256": "当前content.json原始字节SHA256",
  "reviewer": "实际检查者",
  "reviewer_identity": "身份、使用的AI工具及本次审查范围",
  "reviewer_type": "ai_assisted",
  "reviewed_at": "实际时间，须含时区",
  "status": "ready_for_draft",
  "evidence_file": "evidence/writing_review.md",
  "evidence_sha256": "实际留存审读记录原始字节SHA256",
  "checks": {
    "claims": {"status": "reviewed", "passage": "sections/0/paragraphs/0", "quote": "当前该段的实际原文，不少于8字符", "finding": "结合证据具体解释主张及适用边界"},
    "structure": {"status": "reviewed", "passage": "sections/0/paragraphs/0", "quote": "当前该段的实际原文，不少于8字符", "finding": "具体解释本篇结构如何由材料推进"},
    "language": {"status": "reviewed", "passage": "sections/0/paragraphs/0", "quote": "当前该段的实际原文，不少于8字符", "finding": "指出本轮删改及仍须保留的限定"},
    "ending": {"status": "reviewed", "passage": "sections/实际末节/paragraphs/实际末段", "quote": "当前结尾实际原文，不少于8字符", "finding": "解释结论是否得到材料支持"}
  },
  "unresolved_blockers": []
}
```

`passage`来自 `scripts.editorial_policy.passages(content)`，不是文件行号。引用必须属于当前相应位置。没有完成审查就不填 `ready_for_draft`，有阻断就保留在列表并返工，不为凑通过伪造记录。

## 系统实际保证与边界

- 写作API每次加载同一政策；选题API返回当前政策版本及指纹。政策缺失时不降级使用旧提示。
- pipeline check扫描读者可见标题、段落、可选卡片和图注，拦截已确认的旧栏目/精确空泛套话；普通修辞密度仅提醒。扫描不是语义判官，换词或新的空话仍需编辑审读。
- 严格delivery_gate核编辑复核；CLI push在上传前执行严格门禁；远端mp_ops在任何实际add/update前再次验证，并将提交标题和正文逐字对照已复核源稿的受控渲染，不能拿合格源稿替另一份待上传正文背书。已成功且payload一致的幂等重放只回放历史回执，不产生新写入。
- 构建清单绑定政策和扫描代码；源稿或政策改变让复核失效，模板改变让构建失效。图片已有CDN缓存时也传送当前正文、复核记录及其证据。
- 机器核的是记录完整性、版本和引用定位，不能认证审查人的判断必然正确，更不能认证原创性、版权或平台合规。facts/legal/图片许可门禁继续独立执行。
- `tests/editorial_fixtures.py`仅生成临时测试身份，不用于生产批准。旧探针中的transport隔离夹具不算新增编辑门禁的全链路覆盖。

## 多agent与历史资产

旧文章组装脚本内的成篇文案仅是历史产物，不是下一篇写作规范。同一稿件只保留一个写入方；活动线程交接后才能返工原稿。保留旧版快照，不批量修改事实/法务批准、draft_meta或线上草稿。部署、测试、实际成稿与官方入库分别报告。
