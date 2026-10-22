"""PG 环境下的租户隔离验收脚本。

为什么必须是独立脚本、而不是 pytest 用例：
pytest 用 postgres（超级用户）建库跑测试，而**超级用户和 BYPASSRLS 角色会绕过 RLS**，
在那种连接下「RLS 生效」这件事根本测不出来 —— 会得到一棵看起来全绿、实际没验证的树。

这个脚本用两个真实角色连接：
* ``app_rw``     —— 非超级用户、表 owner 不是它 → RLS 必然生效
* ``app_worker`` —— BYPASSRLS → 必须能跨租户（否则 worker 停摆）

用法：
    python scripts/verify_pg_rls.py
    PG_RW_DSN=... PG_WORKER_DSN=... python scripts/verify_pg_rls.py
"""

from __future__ import annotations

import os
import sys

import psycopg

RW_DSN = os.environ.get(
    "PG_RW_DSN", "postgresql://app_rw:app_rw_password@127.0.0.1:55432/competitor"
)
WORKER_DSN = os.environ.get(
    "PG_WORKER_DSN", "postgresql://app_worker:app_worker_password@127.0.0.1:55432/competitor"
)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def _scalar(conn, sql: str, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        row = cur.fetchone()
    return row[0] if row else None


def _ensure_tenant_b_fixture(tenant_b: str) -> str:
    """给 B 造成一条数据（用 worker 身份写，绕过 RLS），否则第 ③ 条无从验证。"""
    with psycopg.connect(WORKER_DSN, autocommit=True) as worker, worker.cursor() as cur:
        cur.execute(
            """INSERT INTO suggestion (id, tenant_id, kind, priority, card, status,
                                           quality_gate, degraded, created_at)
                   VALUES (gen_random_uuid(), %s, 'A_info', 'P2', '{}'::jsonb, 'pending',
                           'pass', false, now())
                   RETURNING id""",
            [tenant_b],
        )
        return str(cur.fetchone()[0])


def _cleanup(fixture_id: str) -> None:
    with psycopg.connect(WORKER_DSN, autocommit=True) as worker, worker.cursor() as cur:
        cur.execute("DELETE FROM suggestion WHERE id = %s", [fixture_id])


def main() -> int:
    with psycopg.connect(RW_DSN, autocommit=True) as rw:
        # 取两个租户的 id（tenant 表无 RLS，web 侧也需要读它）
        with rw.cursor() as cur:
            cur.execute("SELECT id, name FROM tenant ORDER BY created_at LIMIT 2")
            rows = cur.fetchall()
        if len(rows) < 2:
            print("需要至少两个租户才能验证隔离，请先 python manage.py seed_demo")
            return 2
        (tenant_a, name_a), (tenant_b, name_b) = rows[0], rows[1]
        print(f"租户 A={name_a} ({tenant_a})  租户 B={name_b} ({tenant_b})\n")

        # ① 无租户上下文 → fail-closed，一行都不许看到
        count = _scalar(rw, "SELECT count(*) FROM suggestion")
        check("① 无上下文时 app.suggestion 返回 0 行（fail-closed）", count == 0, f"count={count}")

        # ② 设 A 上下文 → 只看到 A
        with rw.transaction(), rw.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_a)])
            cur.execute("SELECT count(*), count(*) FILTER (WHERE tenant_id <> %s) FROM suggestion", [tenant_a])
            total_a, foreign_a = cur.fetchone()
        check("② 设 A 上下文后只看到 A 的数据", foreign_a == 0 and total_a >= 0, f"总数={total_a} 越界={foreign_a}")

        # ③ 按 B 的行 id 直接查 → 0 行（不暴露存在性）
        b_id = _ensure_tenant_b_fixture(str(tenant_b))
        with rw.transaction(), rw.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_a)])
            cur.execute("SELECT count(*) FROM suggestion WHERE id = %s", [b_id])
            leaked = cur.fetchone()[0]
        check("③ 跨租户按 id 查询返回 0 行（不泄露存在性）", leaked == 0, f"B 的行 id={b_id[:8]}… leaked={leaked}")

        # ④ 在 A 上下文里写 B 的行 → WITH CHECK 必须拒绝
        rejected = False
        detail = ""
        try:
            with rw.transaction(), rw.cursor() as cur:
                cur.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant_a)])
                cur.execute(
                    """INSERT INTO suggestion (id, tenant_id, kind, priority, card, status,
                                                   quality_gate, degraded, created_at)
                           VALUES (gen_random_uuid(), %s, 'A_info', 'P2', '{}'::jsonb, 'pending',
                                   'pass', false, now())""",
                    [tenant_b],
                )
        except psycopg.errors.InsufficientPrivilege:
            rejected = True
            detail = "new row violates row-level security policy"
        check("④ 在 A 上下文里插入 B 的数据被拒绝（WITH CHECK）", rejected, detail)

        # ⑤ 事务结束后上下文清除 → 又回到 0 行（SET LOCAL 不泄漏到连接池）
        count_after = _scalar(rw, "SELECT count(*) FROM suggestion")
        check("⑤ 事务结束后上下文自动清除（SET LOCAL 不串租户）", count_after == 0, f"count={count_after}")

        # ⑥ 共享层：可读
        targets = _scalar(rw, "SELECT count(*) FROM target")
        check("⑥ 共享层（shared.target）可读", targets is not None and targets >= 0, f"targets={targets}")

        # ⑦ 共享层：不可改历史（快照不可变靠权限兜底）
        immutable = False
        detail = ""
        try:
            with rw.transaction(), rw.cursor() as cur:
                cur.execute("UPDATE snapshot SET status = 'valid' WHERE id = (SELECT min(id) FROM snapshot)")
                # 能执行到这一行就说明权限没锁住 —— 不拿 rowcount 判断，否则「表里没数据」会假 PASS
                immutable = False
        except psycopg.errors.InsufficientPrivilege:
            immutable = True
            detail = "permission denied for table snapshot"
        check("⑦ app_rw 不能 UPDATE 共享层快照（不可变）", immutable, detail)

        # ⑨ 需求投影表必须允许 web 更新，否则「添加监控对象 / 改优先级」在生产 100% 失败
        can_update_demand = False
        detail = ""
        try:
            with rw.transaction(), rw.cursor() as cur:
                cur.execute(
                    "UPDATE shared.target_demand SET min_interval_min = min_interval_min "
                    "WHERE target_id = (SELECT target_id FROM shared.target_demand LIMIT 1)"
                )
                can_update_demand = True
        except psycopg.errors.InsufficientPrivilege as exc:
            detail = str(exc)[:80]
        check("⑨ app_rw 可更新需求投影（target_demand）", can_update_demand, detail)

        # ⑩ 历史不可变：事件也不能删
        event_locked = False
        detail = ""
        try:
            with rw.transaction(), rw.cursor() as cur:
                cur.execute("DELETE FROM change_event WHERE id = (SELECT max(id) FROM change_event)")
        except psycopg.errors.InsufficientPrivilege as exc:
            event_locked = True
            detail = str(exc)[:60]
        check("⑩ app_rw 不能删变化事件（历史不可变）", event_locked, detail)

    # ⑧ worker 必须能跨租户（否则队列停摆）
    with psycopg.connect(WORKER_DSN, autocommit=True) as worker:
        no_ctx = _scalar(worker, "SELECT count(*) FROM suggestion")
        tenants = _scalar(worker, "SELECT count(DISTINCT tenant_id) FROM suggestion")
    check(
        "⑧ app_worker（BYPASSRLS）能跨租户读写",
        no_ctx is not None and tenants is not None and tenants >= 2,
        f"rows={no_ctx} tenants={tenants}",
    )
    _cleanup(b_id)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        print("失败项：" + "；".join(r[0] for r in failed))
        return 1
    return 0


def _scalar_privileged(sql: str, params) -> str | None:
    """用 worker 角色取 B 的 id（绕过 RLS，仅用于构造测试数据）。"""
    with psycopg.connect(WORKER_DSN, autocommit=True) as worker, worker.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return str(row[0]) if row else None


if __name__ == "__main__":
    sys.exit(main())
