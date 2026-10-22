# ADR-001：表名用纯名 + `search_path`，不用 schema 限定名

- 日期：2026-10-21
- 状态：已接受
- 影响：`core/db.py`、`config/settings.py`、`shared/migrations/0002`

## 背景

架构文档要求两个 schema：`shared`（L1 公开快照，无租户列）与 `app`（L2 租户数据 + RLS）。
最初的实现让模型 `db_table` 按后端拼名字：PG 下返回 `"shared"."snapshot"`，SQLite 下返回 `shared_snapshot`。

问题：`db_table` 会被写进 migration 文件。开发库（SQLite）与生产库（PG）会生成两套不同表名的迁移，
同一个 commit 在两处跑出不同 schema，早晚打架；而且 `makemigrations` 用的是当前后端，换个环境生成就漂了。

## 决策

1. 两个 schema 里的表名**全局唯一**（`target` / `snapshot` / `suggestion` …），模型只写纯表名。
2. PG 连接带 `options=-c search_path=app,shared,public`，由 search_path 解析归属。
3. 建表后由 `shared/migrations/0002_pg_schema_layout` 一次性把表 `ALTER TABLE ... SET SCHEMA` 搬到对应 schema。
4. `tenancy/migrations/0002` 在其后装 RLS（`ENABLE` + `FORCE` + fail-closed 策略）。

## 后果

- ✅ 开发（SQLite）与生产（PG）共用同一套迁移，`makemigrations` 不再依赖后端
- ✅ 迁移仍是普通 Django 迁移，可回滚（`backwards` 把表搬回 public）
- ⚠️ **新增 shared 层表时**必须同步把表名加进 `0002_pg_schema_layout.SHARED_TABLES`，
  否则会被建到 `app`（search_path 第一位）。这条已写进迁移文件顶部注释与 CI 检查
- ⚠️ SQLite 下没有 schema 概念，隔离只靠 ORM 默认管理器（见 README「已知限制」）

## 反对意见

「直接用 schema 限定名更直观。」——直观，但会把「迁移文件依赖运行时后端」这个坑埋进代码库，
且这个坑只在换环境那一刻爆，属于单人项目最不该留的隐患。
