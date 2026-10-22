# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

## [0.2.0] - 2026-10-22

一轮**独立代码审查**驱动的大修（4 Blocker / 15 Major / 31 Minor），外加四项能力补齐。
审查记录：`docs/代码审查记录.md`；发布检查表：`docs/发布检查表.md`。

### Fixed — Blocker（都会在生产直接失效，SQLite 下测不出）

- 促销-only 事件（券上线/下线）走价格提示卡时 `price_delta=None` → `AttributeError` → 分析任务进死信，
  租户**一条建议都收不到**；结论改为三段拼装并容忍 None
- 熔断目标**永不恢复**：`open → half_open` 只在拿到采集任务时才发生，而 open 不进调度 →
  一次 429 风暴永久踢出监控；调度条件补「冷却到期」分支并落 `half_open`
- web 进程（`app_rw`）更新 `shared.target`/`target_demand` 被拒 → 生产「添加监控对象/改优先级/解绑」100% 500；
  新增 `shared/0006` 精确授权（快照仍只读），隔离验收加写路径检查
- `health_snapshot`/`silence_check`/`daily_digest` **从未被投递** → 保留期清理、沉默告警、指标看板全都不存在；
  `Worker._maybe_periodic()` 按小时/按日报小时投递（幂等键防重）

### Fixed — Major

- `SET LOCAL` 在嵌套 savepoint 中不会自动回滚 → `audit()` 会污染外层租户上下文；改为显式恢复
- 幂等键 check-then-create 竞态 → 撞唯一约束时按幂等返回；采集/分析不再重复
- worker 无背压：任务会被提前标 running 却未执行，租约到期被回收 → **同一任务并发跑两遍**
- 任务回收阈值固定 300s → 长任务被误杀；改为按类型租约（`Job.lease_until`，日报 60 分钟）
- worker 主线程连接不回收 → DB 重启后永久卡死
- LLM 记账失败连坐业务结果；4xx 全部重试；`LLM_PROMPT_VERSION` 是死配置且改错会让分析任务全灭
- 成员表单未校验角色 → 可自造 `platform_ops` 账号（横向提权）
- 登录 `next` 参数开放重定向（`//evil.com` 可通过）
- CSV 导出公式注入（`=`/`+`/`-`/`@` 开头会被 Excel 执行）
- `read_raw()` 路径穿越（潜在任意文件读取）
- 含可空列的唯一约束在 PG 默认 `NULLS DISTINCT` → 幂等键失效；4 个约束改 `nulls_distinct=False`
- 成本 ≥ 售价时输出「单件让利上限 -122 元」；改为「成本数据异常，请核对」
- 券门槛用循环外旧 `payable`，平台券扣完后店铺券门槛失真 → **系统性低估到手价**

### Added

- **租户级 LLM 用量与配额**：`llm_usage` 表（开 RLS）+ 配额改为「每租户/日」，一个租户打满不影响别人；
  设置页显示今日/本月用量与成本
- **判据命令 `manage.py gate_report`**：到手价可确定率、降级率、8 字段齐全率、证据回链率、采纳率、P95 时延，
  并给出「B 类闸门能否打开」的结论（`--gate` 可作 CI 门禁）
- **采集探针 `manage.py probe_page`**：打印强标记命中、字段、促销明细、到手价求解过程；
  `--save` 存为回归样本。接新类目/合规评审的第一件工具
- **权限矩阵测试**：4 角色 × 8 动作 = 34 格，期望写进测试数据
- **备份/恢复脚本** `scripts/backup.sh` / `restore.sh`：含行数清单与校验和，恢复后自动核对
- **CI** `.github/workflows/ci.yml`：ruff → SQLite 测试 → PG 迁移与授权 → 双后端测试 → 隔离验收 → 判据报告
- **任务租约** `Job.lease_until` + worker 在飞任务背压
- 事件去抖落地：30 分钟内同类变化合并为同一事件（价格往复不刷屏），分析幂等键带上快照 ID 以刷新卡片
- 抓不到价时清空 `last_price_*`（不再拿过期价格算竞争力）
- `/ops/health` 增加「B 类闸门」与「数据体积与保留期」两块看板

### Changed

- 日报收件人改为**仅平台运营**（正文含全平台聚合，不能发给每个租户管理员）
- `seed_demo` 在 `APP_ENV=prod` 下拒绝执行；`is_staff` 恒 False（平台运营不进 Django admin）
- `purge_finished` 默认 30 天（避免过早释放幂等键）；`queue_stats` 移除永远为 0 的 `failed`
- 能力规格如实改写：N1（含证据子串校验）**未接入生产链路**，生产生效的是「schema + 证据回链」两道门

### Verified

| 项 | 结果 |
|---|---|
| `ruff check .` | All checks passed |
| 测试（SQLite） | 211 passed, 3 skipped |
| 测试（PG 16.15） | 214 passed |
| 隔离验收 `scripts/verify_pg_rls.py` | 10/10（含写路径 ⑨、历史不可变 ⑩） |
| 备份 → 恢复 → 核对 | 5 张表行数逐项一致 |

## [0.1.2] - 2026-10-22

技术栈核对（对照 market-tech-stack-atlas）+ 数据生命周期落地。核心结论：四项核心与图鉴
P4（多租户 B2B SaaS）默认完全一致，三项有意偏离，三项真缺口（SSO / 错误追踪 / 按租户成本视图）。

### Added

- `SNAPSHOT_RETENTION_DAYS`（默认 400）+ `shared/maintenance.py`：批量清理过期快照与变化事件，
  **先删事件再删快照**（`ChangeEvent.snapshot` 是 DO_NOTHING 外键，顺序错会撞约束）
- `shared/migrations/0005_brin_indexes`：`captured_at` / `detected_at` 上建 BRIN 索引
  （`pages_per_range=32`，体积约为 B-tree 的 1%，只对 PG 生效）
- `/ops/health` 新增「数据体积与保留期」卡片：每张表的行数/索引体积/总体积 + 分区判读
- 每日自检邮件增加清理结果与快照表规模
- `docs/adr/005-snapshot-partitioning.md`：**明确暂不做分区**，写死触发阈值（2,000 万行 / 50 GB）与四步预案
- `docs/技术栈核对.md`：逐技术位对照 + 三项偏离的理由 + 三项缺口 + 规模化拐点 + 时效声明
- 按图鉴维护约定，把 RLS 多租户、django-tenants、BRIN、PgBouncer、数据库任务表做队列、structlog
  六条补进 atlas 对应 reference 的 `## 待并入`

### Verified

| 项 | 结果 |
|---|---|
| 测试（SQLite） | 137 passed, 2 skipped（PG-only 用例） |
| 测试（PG 16.15） | 139 passed |
| `ruff check .` | All checks passed |
| `/ops/health` 体积卡片 | 显示 `shared.snapshot` 等表体积，并给出「未到分区阈值」判读 |
| 每日清理实跑（app_worker） | `purged={'snapshots': 0, 'events': 0}`，保留期 400 天 |

## [0.1.1] - 2026-10-21

在真实 PostgreSQL 16.15 上完成隔离验收（此前只在 SQLite 上跑通，RLS 属于「已写未验」）。

### Added

- `scripts/verify_pg_rls.py`：用真实角色（`app_rw` 受 RLS、`app_worker` BYPASSRLS）验收 8 项隔离行为，
  **超级用户跑 pytest 测不出 RLS**，必须有这个独立入口
- `shared/migrations/0004_shared_readonly_grants`：共享层显式只读（`REVOKE UPDATE/DELETE/TRUNCATE`）
- `docs/PG验收记录.md`：迁移布局、8 项结果、真实角色下的业务链路、6 个只在真库暴露的问题
- `deploy/init/01_roles.sql`：先建 schema 再授权，并给两个角色设默认 `search_path`

### Fixed（全部只在「非超级用户 + 真库」下暴露）

- initdb 失败：授权语句早于 schema 创建（容器 psql 带 `ON_ERROR_STOP=1`，一条错整体退出）
- 迁移报 `column "tenant_id" does not exist`：`metric_daily` 无租户列却被纳入 RLS 清单
- 表未搬进 `shared`：`0002` 假设表都在 `public`，但当 schema 预先存在时建表落在 `app` → 改为按 `pg_tables` 查实际 schema
- 「快照不可变」失效：`ALTER DEFAULT PRIVILEGES` 按建表时 schema 记账，表搬 schema 后仍带 UPDATE 权限
- `app_rw` 下登录 500：登录后写审计时租户上下文尚未建立，RLS `WITH CHECK` 拒绝插入 →
  `audit()` 显式套租户上下文；无租户的全局事件只落结构化日志；审计写失败不连坐业务请求
- RLS 策略 `''::uuid` 报 `22P02`：事务结束后 `current_setting` 可能残留空字符串 → 用 `NULLIF(..., '')::uuid`

### Verified

| 项 | 结果 |
|---|---|
| 隔离验收 `scripts/verify_pg_rls.py` | 8/8 |
| 测试（SQLite / PG） | 132 passed / 132 passed |
| `ruff check .` | All checks passed |
| Web 在 `app_rw` 下（登录/6 页/采纳/新增商品/导出） | 全通，无 500 |
| worker 在 `app_worker` 下（3 采集 → 3 事件 → 3 建议） | 全通 |

## [0.1.0] - 2026-10-21

首个可运行版本：按《架构设计文档 v1.0》实现「采集 → 抽取 → 到手价 → diff → 事件 → 建议 → 推送 → 反馈」闭环。

### Added

**核心链路**
- 模块化单体（Django 5 + HTMX），`web` 与 `worker` 两进程共享代码与库
- PG 任务表作为队列：`SKIP LOCKED` 抢占、幂等键、指数退避（1/5/25s）、死信、卡死回收
- 采集：合规护栏（仅 https / 域名白名单 / 无 Cookie / robots）、令牌桶按额度分批放行、熔断状态机落库
- 条件请求（ETag / Last-Modified → 304 不落快照）、尊重 `Retry-After` 与 `Crawl-delay`
- 异常页识别 6 类：`HTTP_ERROR / RATE_LIMITED / LOGIN_WALL / RISK_CONTROL / EMPTY_BODY / STRUCTURE_CHANGED`
- 规则抽取：6 个强结构标记、标题/价格/主图/店铺/SKU/属性/促销区
- 到手价求解：秒杀 → 满减/每满减 → 立减 → 券，逐项明细；冲突标 `uncertain`，绝不估算
- 变化检测：内容指纹、字段级 diff、30 分钟抖动合并、只对实质变化进分析
- 分析：N1 促销抽取 + N2 归因/影响/建议（单次调用），三道硬校验（证据子串、证据回链、规则回算）
- 建议卡：8 字段强制 schema、合规词拦截、质量门禁 `pass/manual_required`
- 通知：企微 → 邮件 → 站内三级降级，幂等键 `(suggestion, channel, hour)`
- 反馈闭环：采纳转任务、忽略必填原因、误报回流评测

**隔离与安全**
- `shared` / `app` 双 schema，表名全局唯一 + `search_path` 解析（ADR-001）
- PG RLS：`ENABLE` + `FORCE` + `current_setting('app.tenant_id', true)` fail-closed
- 应用层：ORM 默认管理器按上下文过滤，未设上下文返回 0 行
- 中间件拒绝任何请求参数里的 `tenant_id`（400 + 审计），跨租户对象统一 404
- session 认证（httpOnly + SameSite=Lax）、登录失败 5 次锁 15 分钟（计数落库）
- 平台运营账号物理上进不了租户上下文，业务页面 403
- 导出 CSV 带水印（租户/操作人/时间/范围）并写审计

**界面（9 个页面，无 CDN 依赖）**
- 工作台（P0/P1/采集健康/待办 + 30 秒 HTMX 轮询）、监控列表、竞品详情（到手价明细/时间轴/快照）
- 建议中心（筛选/抽屉/采纳/忽略/误报/导出）、任务看板、自有商品、设置、平台采集健康

**可观测**
- structlog JSON 日志 + `trace_id` 全链路透传
- `metric_daily` 小时指标 + `/ops/health` 页面 + 每日自检邮件（含清理任务）
- 沉默告警：有活跃目标但 6 小时零变化事件

**交付**
- `Dockerfile` + `compose.yaml`（db / init / web / worker）+ PG 角色与授权脚本
- `seed_demo` 演示数据（含「变化前」基线快照，一跑就能看到真实变化与建议）
- 132 个测试：单元 / 集成 / 评测门禁（越权 6 条、到手价一致率、证据子串、幻觉计数）

### Decisions

- ADR-001 表名用纯名 + `search_path`，不用 schema 限定名
- ADR-002 任务表放 `shared` 层且不开 RLS（worker 需跨租户）
- ADR-003 一期主打 A 类，B 类（跟价）默认不自动推送（`B_CLASS_ENABLED=0`）
- ADR-004 采集加条件请求 / `Retry-After` / `Crawl-delay`

### Known limitations

- 本机无 PostgreSQL：开发用 SQLite 跑通全链路，该模式下 `app.*` **没有 RLS**（仅 ORM 层隔离）；
  RLS 迁移已就绪，需在 PG 环境复核越权用例
- 快照未按月分区（单表 + 索引 + 保留期清理）
- 采集默认 `fixture` 模式；LLM 默认关闭（规则归因）
- 未实现：效果追踪看板、批量导入、套餐计费、站内消息中心

### Fixed（开发阶段发现并修掉的真问题）

- 令牌桶容量为 1 时永远拿不到多个额度，调度空转 → 改为按可用额度分批放行
- 「每满200减30」被「满200减30」重复命中，误判为规则冲突 → 加负向先行断言
- 满减门槛未达 / 券门槛未达被误标为「不确定」→ 改为确定性结论
- `304` 落在 3xx 区间，被「重定向一律拦截」误伤 → 显式列举重定向状态码
- 标题词差集用 n-gram 会产出「分袖」「季宽」这类碎片 → 改为商品词表命中
- 自定义错误中间件在 `__call__` 里 catch 不到视图异常（Django 已先行转换为响应）→ 改用 `process_exception`
