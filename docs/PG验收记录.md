# PostgreSQL 验收记录

- 日期：2026-10-21
- 环境：Docker 29.6.1 / PostgreSQL **16.15** (linux/amd64-alpine)，宿主机端口 `55432`
- 角色：`app_rw`（受 RLS 约束，web 进程）、`app_worker`（BYPASSRLS，worker 进程）
- 结论：**8/8 隔离检查通过；双后端（SQLite / PG）132 个测试全绿；Web 与 worker 在真实角色下跑通全链路**

为什么单独做这份记录：用超级用户跑 pytest **永远测不出 RLS**（超级用户和 BYPASSRLS
角色绕过一切策略）。本项目之前的「已知限制」就是「RLS 未在真库验证」，这次把它补上，
并记录过程中暴露的 5 个真问题 —— 它们全都只在「非超级用户 + 真库」下才会现形。

## 一、迁移与布局

```
python manage.py migrate --noinput     # 全部 OK（含 shared.0002 搬 schema、tenancy.0002 装 RLS、shared.0004 锁权限）
```

| schema | 内容 |
|---|---|
| `shared` | `target, target_demand, snapshot, change_event, llm_cache, job, rate_bucket` |
| `app` | 业务表 14 张 + `auth_* / django_* / app_user_groups` 等框架表 |
| `public` | 空（表已全部归位） |

RLS 状态（`pg_class.relrowsecurity / relforcerowsecurity`）：

| 开了 RLS + FORCE | 刻意不开（有理由） |
|---|---|
| `self_product, tenant_target, event_priority, analysis, suggestion, task, feedback, notification, audit_log, analytics_event` | `tenant / app_user / login_throttle`（登录要在知道租户之前按 email 查人）<br>`metric_daily`（全局运维指标，无租户列；配额是一期全局口径） |

## 二、隔离验收（`python scripts/verify_pg_rls.py`）

```
PASS ① 无上下文时 app.suggestion 返回 0 行（fail-closed）            count=0
PASS ② 设 A 上下文后只看到 A 的数据                                  总数=3 越界=0
PASS ③ 跨租户按 id 查询返回 0 行（不泄露存在性）                     leaked=0
PASS ④ 在 A 上下文里插入 B 的数据被拒绝（WITH CHECK）                new row violates RLS policy
PASS ⑤ 事务结束后上下文自动清除（SET LOCAL 不串租户）                count=0
PASS ⑥ 共享层（shared.target）可读                                   targets=3
PASS ⑦ app_rw 不能 UPDATE 共享层快照（不可变）                       permission denied
PASS ⑧ app_worker（BYPASSRLS）能跨租户读写                           rows=4 tenants=2
```

## 三、真实角色下的业务链路

| 场景 | 角色 | 结果 |
|---|---|---|
| 登录（正确密码 / 错误密码） | `app_rw` | 302 → 工作台 / 200 + 「邮箱或密码不正确」，**均无 500** |
| 6 个页面 GET（工作台/监控/建议/任务/商品/设置） | `app_rw` | 全 200 |
| 采纳建议（写 suggestion + task + feedback + audit） | `app_rw` | 204，任务与审计均落库 |
| 新增自有商品 | `app_rw` | 302，商品数 +1 |
| 导出 CSV（带水印 + 审计） | `app_rw` | 200，水印含租户/操作人/时间 |
| `drain_queue --tick`（3 次采集 + 3 次分析） | `app_worker` | 3 事件 → 3 建议，无权限错误 |
| 双后端测试 `pytest -q` | postgres / SQLite | 132 passed / 132 passed |
| `ruff check .` | — | All checks passed |

## 四、过程中修掉的 5 个问题（都只在真库暴露）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | PG 容器 initdb 直接退出 | `01_roles.sql` 先 `GRANT USAGE ON SCHEMA shared`，而两个 schema 那时还不存在；容器 psql 带 `ON_ERROR_STOP=1`，一条错就整体失败 | 脚本开头 `CREATE SCHEMA IF NOT EXISTS`；并 `ALTER ROLE ... SET search_path` 兜底 |
| 2 | 迁移报 `column "tenant_id" does not exist` | `metric_daily` 是全局指标表，**本来就没有租户列**，却被列入 RLS 清单 | 从 RLS 清单移除，并在迁移里写明「不开 RLS 的表及理由」 |
| 3 | 表没搬进 `shared`（初始化顺序不同时） | `0002` 假设表都在 `public`；但若 schema 预先存在，建表会落到 search_path 第一位（`app`），搬移语句 `ALTER TABLE public.x` 变成空操作 | 改为按 `pg_tables` 查**实际所在 schema** 再搬，两种顺序都能跑 |
| 4 | 「快照不可变」没生效：`app_rw` 能 UPDATE `snapshot` | `ALTER DEFAULT PRIVILEGES` 只在建表那一刻按所在 schema 记账；表先建在 `app` 再 `SET SCHEMA shared`，权限跟着表走 | 新增 `shared/0004_shared_readonly_grants`：显式 `REVOKE UPDATE/DELETE/TRUNCATE` + `GRANT SELECT/INSERT` |
| 5 | 登录 500（`app_rw` 下） | 登录成功后写审计时租户上下文还空着（中间件看到的是匿名用户），RLS `WITH CHECK` 拒绝插入 | `audit()` 显式给这条写入套一次租户上下文；无租户的全局事件（匿名登录失败）只落结构化日志，且审计写失败不连坐业务请求 |
| 6 | RLS 策略在 `SET LOCAL` 之后可能抛 `22P02` | 事务结束后 `current_setting('app.tenant_id')` 可能残留空字符串，`''::uuid` 直接报错，把「拒绝」变成 500 | 策略改用 `NULLIF(current_setting(..., true), '')::uuid` |

## 五、仍然存在的差异（不影响隔离，但要知道）

1. `auth_* / django_* / app_user_groups` 等框架表落在 `app` schema（因为 search_path 第一位是 `app`）。它们没有业务数据，也未开 RLS；移动它们会牵动外键，收益不大。
2. `snapshot` 未按月分区：当前单表 + 索引 + 保留期清理。分区迁移需在 PG 上单独验证（`CREATE TABLE ... PARTITION BY RANGE` + 迁移现有数据），未在本次范围内。
3. 本记录的 PG 是容器实例，端口/口令均为测试值；生产用 `compose.yaml` 里的 `POSTGRES_PASSWORD`/`APP_RW_PASSWORD`/`APP_WORKER_PASSWORD`。

## 六、复现命令

```bash
# 1. 起库（含角色与授权）
docker run -d --name pg-competitor -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=competitor \
  -p 55432:5432 -v "$PWD/deploy/init:/docker-entrypoint-initdb.d:ro" postgres:16-alpine

# 2. 迁移（owner 角色）
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/competitor python manage.py migrate

# 3. 演示数据 + 跑一轮闭环
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/competitor python manage.py seed_demo
DATABASE_URL=postgresql://app_worker:app_worker_password@127.0.0.1:55432/competitor \
  python manage.py drain_queue --tick

# 4. 隔离验收（8 项）
python scripts/verify_pg_rls.py

# 5. 双后端测试
pytest -q
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/competitor pytest -q
```
