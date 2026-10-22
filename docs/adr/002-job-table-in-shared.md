# ADR-002：任务表放在 `shared`，且不开 RLS

- 日期：2026-10-21
- 状态：已接受
- 影响：`shared/models.py::Job`、`jobs/queue.py`、`tenancy/migrations/0002`

## 背景

架构文档 §5.4 把 `job` 列在 `app` 层（租户层）。但 worker 是**单进程、跨租户**工作：
它要扫所有租户的 target、把所有租户的分析任务都跑掉。若 job 表在 `app` 层并开 RLS，
worker 在没有租户上下文时一行也取不到 —— 队列直接停摆。

另一种解法是 worker 逐个租户设上下文再取任务，但那会让「一个全局队列」退化成
「N 个租户队列 + 轮询」，既慢又容易饿死某个租户。

## 决策

1. `job` 表放在 `shared` 层，**不含任何租户业务数据**：payload 只放 ID 引用（event_id / tenant_id / product_id）。
2. 不开 RLS：worker 用带 `BYPASSRLS` 的 `app_worker` 账号访问，web 用受 RLS 约束的 `app_rw`。
3. `jobs` 的审计与限额不靠 RLS，靠「payload 里没有业务内容」这条约束（写测试守着）。

## 后果

- ✅ 单队列 + 单 worker，调度简单：`ORDER BY priority, run_after LIMIT n FOR UPDATE SKIP LOCKED`
- ✅ 排障只需一条 SQL：`SELECT kind, state, count(*) FROM shared_job GROUP BY 1,2`
- ⚠️ `shared` 层的「不得含租户列」规则对 `job` 是个例外（它有 `payload.tenant_id` 作为 JSON 字段）。
  CI 的白名单里显式放行 `job`，并在注释里写明「payload 只允许 ID 引用」
- ⚠️ 若未来把 worker 拆成按租户分片，需要重新评估这条决策

## 反对意见

「把 job 放 app 层更符合分层。」——符合纸面分层，但会让「跨租户调度」这个核心动作
每次都要临时提权，反而制造了更多绕过隔离的机会。
