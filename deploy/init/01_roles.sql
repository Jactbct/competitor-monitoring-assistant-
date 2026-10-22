-- 私有化 / SaaS 通用的数据库账号与权限（由 postgres 容器首次启动时自动执行）
--
-- 为什么是两个账号：
--   app_rw     —— web 进程用。对 shared 只有 SELECT/INSERT（快照不可变靠权限保证），
--                 对 app 全权限但受 RLS 约束（必须带 app.tenant_id）。
--   app_worker —— worker 进程用。需要跨租户扇出与汇总，必须 BYPASSRLS。
-- 这条分工是「隔离红线不靠代码纪律」的物理基础，不要合并成一个账号。
--
-- 注意顺序：schema 必须先存在，否则 GRANT USAGE 会直接让 initdb 失败
-- （容器的 psql 带 ON_ERROR_STOP=1，一条错就整体退出）。

CREATE SCHEMA IF NOT EXISTS shared;
CREATE SCHEMA IF NOT EXISTS app;

CREATE ROLE app_rw     LOGIN PASSWORD 'app_rw_password';
CREATE ROLE app_worker LOGIN PASSWORD 'app_worker_password' BYPASSRLS;

GRANT CONNECT ON DATABASE competitor TO app_rw, app_worker;
GRANT USAGE ON SCHEMA public TO app_rw, app_worker;
GRANT USAGE ON SCHEMA shared TO app_rw, app_worker;
GRANT USAGE ON SCHEMA app TO app_rw, app_worker;

-- 让两个账号即使不显式传 search_path 也能解析到两张 schema
-- （Django 侧仍会通过 OPTIONS 显式设置，这里是兜底）
ALTER ROLE app_rw     IN DATABASE competitor SET search_path = app, shared, public;
ALTER ROLE app_worker IN DATABASE competitor SET search_path = app, shared, public;

-- 共享层：只读 + 只插入，不能改历史
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA shared TO app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA shared TO app_rw;

-- 例外：target / target_demand 是「需求投影」，web 侧改优先级/解绑时要更新；
-- 快照、事件、任务表仍严格只读（不可变靠权限兜底）。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='shared' AND tablename='target') THEN
    GRANT UPDATE ON shared.target, shared.target_demand TO app_rw;
  END IF;
END $$;

-- worker 需要推进 next_due_at / 熔断状态 / 令牌桶，所以共享层给全权限
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA shared TO app_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA shared TO app_worker;

-- 租户层：web 全权限（但仍被 RLS 限制在自己的 tenant 内）
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO app_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO app_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO app_worker;

-- 以后新建的表自动带同样的授权（迁移由 postgres 执行，默认权限按 postgres 记）
ALTER DEFAULT PRIVILEGES IN SCHEMA shared
  GRANT SELECT, INSERT ON TABLES TO app_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA shared
  GRANT USAGE, SELECT ON SEQUENCES TO app_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA shared
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA shared
  GRANT USAGE, SELECT ON SEQUENCES TO app_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw, app_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA app
  GRANT USAGE, SELECT ON SEQUENCES TO app_rw, app_worker;

-- 迁移用的 owner 仍是 postgres；表的所有者不是 app_rw，配合 FORCE RLS 才真正生效
