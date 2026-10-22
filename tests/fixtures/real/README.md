# 真实页面样本（人工取数）

## 这里放什么

你自己在浏览器里**手动另存为**保存下来的商品页 HTML。命令会用它们验证解析能力：

```bash
python manage.py probe_page --url "https://item.taobao.com/item.htm?id=真实ID" \
    --fixture tests/fixtures/real/真实ID.html
```

## 为什么是「人工另存」，不是「自动抓登录后的页面」

这不是能力问题，是**产品红线**（架构文档 §10.3、ADR 与合规清单里都写死）：

1. 我们**不处理登录态**：不读 Cookie、不注入脚本、不接管你已登录的浏览器会话；
2. 采集客户端在代码层就只允许 5 个请求头（`user-agent/accept/accept-language/accept-encoding/referer`），
   `cookie`、`authorization` 属于被白名单挡掉的，**加不进去**（`domain/collect/guardrails.py` 的 `HEADER_WHITELIST`）；
3. 用登录态批量取数大概率违反平台用户协议，风险落在你的账号上，这是「省事一分钟、赔上账号」的交易。

## 人工取数的操作步骤

1. 浏览器登录后打开商品详情页；
2. `Ctrl+S` → 保存类型选「网页，仅 HTML」（不要「完整网页」，避免带一堆图片）；
3. 把文件改名成 `<商品ID>.html` 放进本目录；
4. 告诉我文件名，我用 `probe_page` 跑一遍，看强结构标记命中（≥2 才算能解析）。

## 拿到样本之后我会做什么

- 用 `probe_page` 输出：强标记命中数、标题/价格/店铺/SKU/属性、促销明细、到手价求解过程
- 如果结构与现有淘宝 profile 不一致，改 `domain/extract/rules.py` 的 `SiteProfile`（不动 `extract()`）
- 把该样本加进回归测试，避免以后改坏了
- **仍然不会**把你的登录态带进采集流程；真实定时抓取只走公开页（`COLLECTOR_MODE=http` 且需合规评审）

## 合规提醒

即使用人工样本调通了结构，也不代表可以自动批量抓取。上线前仍需要：
确认目标站点 robots 与用户协议、控制频率（本项目默认 1 QPS）、只取公开可见字段、
不采集用户信息与评价原文。这些约束的落点见 `docs/能力规格.md` §5 与 `docs/参考与取舍.md`。
