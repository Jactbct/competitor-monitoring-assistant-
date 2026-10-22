# ADR-004：采集加条件请求、Retry-After、Crawl-delay

- 日期：2026-10-21
- 状态：已接受
- 影响：`domain/collect/client.py`、`domain/collect/guardrails.py`、`jobs/handlers/collect.py`
- 来源：参考同类开源项目（changedetection.io）与抓取工程实践后的取舍，详见 `docs/参考与取舍.md`

## 背景

原实现的每一轮采集都是无条件 GET：即使页面三个月没变，也照样传输几百 KB。
在 1 QPS、8.6 万次/日的预算下，这是最大的可避免浪费，也是最容易让站点把我们当爬虫的理由。

## 决策

1. **条件请求**：`Target` 保存 `http_etag` / `http_last_modified`，下轮带上
   `If-None-Match` / `If-Modified-Since`；收到 304 时**不落快照、不算异常**，
   只更新 `last_304_at` 与 `next_due_at`。
2. **Retry-After**：429/503 时熔断冷却取 `max(默认冷却, Retry-After)`，上限 24h。
3. **Crawl-delay**：解析 robots 的 `Crawl-delay`，采集间隔取 `max(配置间隔, crawl-delay)`。
4. 条件请求头加入护栏白名单（它们只表达「我已有哪个版本」，不携带任何登录态），
   仍然拒绝 `cookie` / `authorization`。

## 后果

- ✅ 未变化页面几乎零成本；带宽与风控暴露面同时下降
- ✅ 站点明确表达频率时我们照做，合规评审更好过
- ⚠️ 304 分支若写错会很危险：把「没变」当成「采集失败」会误熔断；
  把空 body 当有效快照落库会让 diff 全变。已用 `test_not_modified_skips_snapshot` 守住
- ⚠️ 304 也落在 3xx 区间，**不能**被「重定向一律拦截」的逻辑误伤（实现里显式列出 301/302/303/307/308）

## 反对意见

「加了三个分支，采集逻辑变复杂了。」——复杂度换来的是预算与合规，而且三个分支都有独立测试。
不加的话，省下的代码会在「IP 被封」那天连本带利还回来。
