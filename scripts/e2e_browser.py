"""浏览器端 E2E（Playwright + 系统 Edge）。

为什么需要它：G10 要求「端到端跑通，不只是 dev server + curl」。curl 只能证明
HTML 200，证明不了「点查看 → 抽屉弹出 → 点采纳 → 任务出现」这条交互链。

设计取舍：
* 用系统已装的 Edge（``channel="msedge"``），不额外下载 150MB Chromium；
* 流程只覆盖**不可逆的用户路径**：登录、看建议、采纳、看任务、平台视图；
* 每步截图存 ``.data/e2e/``，出问题能直接看画面，不用逐行猜。

用法（服务需先跑起来）：
    python scripts/e2e_browser.py
    BASE_URL=http://127.0.0.1:8000 python scripts/e2e_browser.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
SHOT_DIR = Path(os.environ.get("E2E_SHOT_DIR", ".data/e2e"))
TENANT_USER = os.environ.get("E2E_TENANT_USER", "ops@demo.local")
TENANT_PASS = os.environ.get("E2E_TENANT_PASS", "demo12345")
OPS_USER = os.environ.get("E2E_OPS_USER", "platform@demo.local")

steps: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        detail = fn() or ""
        steps.append((name, True, str(detail)))
        print(f"PASS  {name}  {detail}")
    except Exception as exc:  # noqa: BLE001 - E2E 失败要打印上下文而不是堆栈
        steps.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"FAIL  {name}  {type(exc).__name__}: {exc}")


def shot(page: Page, name: str) -> None:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOT_DIR / f"{name}.png"), full_page=True)


def safe_goto(page: Page, url: str, attempts: int = 3) -> None:
    """htmx 的 HX-Refresh 会触发整页重载；紧接着 goto 会和它抢导航，报 ERR_ABORTED。
    这里等一下再跳，失败重试，避免把「测试时序」当成产品缺陷。"""
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except Exception:  # noqa: BLE001
            if i == attempts - 1:
                raise
            page.wait_for_timeout(400)


def login(page: Page, email: str, password: str) -> None:
    page.goto(f"{BASE_URL}/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def prepare_data() -> None:
    """E2E 会真的点「采纳」，跑几次就把待处理建议消耗光；每次先补齐一条。"""
    if os.environ.get("E2E_PREPARE", "1") not in {"1", "true", "yes"}:
        return
    import subprocess

    result = subprocess.run(
        [sys.executable, "manage.py", "seed_e2e"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    print(f"[prepare] {result.stdout.strip() or result.stderr.strip()[:200]}")


def main() -> int:
    prepare_data()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = context.new_page()

        def s_login():
            login(page, TENANT_USER, TENANT_PASS)
            expect(page.locator(".topbar h1")).to_have_text("工作台")
            return page.title()

        check("登录 → 工作台", s_login)
        shot(page, "01-workbench")

        def s_workbench_cards():  # noqa: D401
            expect(page.locator("#p0-count")).to_be_visible()
            expect(page.locator("#alerts-panel")).to_be_visible()
            p0 = page.locator("#p0-count").inner_text()
            return f"P0={p0}"

        check("工作台卡片与建议列表可见", s_workbench_cards)

        def s_drawer():
            page.goto(f"{BASE_URL}/suggestions")
            first_view = page.locator('button[hx-get][hx-target="#drawer"]').first
            first_view.click()
            expect(page.locator("#drawer.open")).to_be_visible(timeout=5000)
            expect(page.locator("#drawer")).to_contain_text("结论")
            expect(page.locator("#drawer")).to_contain_text("证据")
            # 抽屉有 200ms 位移动画：不等动画结束就截图会拍到「半开」状态
            page.wait_for_timeout(400)
            box = page.locator("#drawer").bounding_box()
            viewport = page.viewport_size
            assert box is not None and viewport is not None
            assert box["x"] + box["width"] <= viewport["width"] + 1, f"抽屉超出视口：{box}"
            shot(page, "02-suggestion-drawer")
            return f"抽屉展开完整（x={box['x']:.0f}, w={box['width']:.0f}）"

        check("建议中心 → 查看 → 抽屉展开", s_drawer)

        def s_accept():
            accept = page.locator('#drawer button:has-text("采纳并转任务")').first
            expect(accept).to_be_visible()
            accept.click()
            page.wait_for_load_state("networkidle")
            # HX-Refresh 会整页刷新，应回到建议列表
            expect(page.locator(".topbar h1")).to_be_visible()
            page.wait_for_timeout(500)  # 给紧随其后的导航留出窗口
            return "已采纳"

        check("在抽屉内采纳建议", s_accept)

        def s_task_exists():
            safe_goto(page, f"{BASE_URL}/tasks")
            expect(page.locator(".card-head h2").first).to_be_visible()
            cards = page.locator(".card-body >> text=开始").count()
            assert cards >= 1, "采纳后任务看板里应至少有一条可执行任务"
            shot(page, "03-tasks")
            return f"任务卡 {cards} 个"

        check("任务看板出现采纳产生的任务", s_task_exists)

        def s_targets_detail():
            page.goto(f"{BASE_URL}/targets")
            link = page.locator("table tbody tr td a[href^='/targets/']").first
            link.click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("text=到手价明细")).to_be_visible()
            shot(page, "04-target-detail")
            return "详情页含到手价明细"

        check("监控列表 → 竞品详情（到手价明细）", s_targets_detail)

        def s_platform_view():
            context.clear_cookies()
            login(page, OPS_USER, TENANT_PASS)
            page.goto(f"{BASE_URL}/ops/health")
            expect(page.locator("text=数据体积与保留期")).to_be_visible()
            expect(page.locator("text=B 类闸门")).to_be_visible()
            shot(page, "05-ops-health")
            return "平台视图含体积与闸门两卡片"

        check("平台运营 → 采集健康（体积 + 闸门）", s_platform_view)

        def s_isolation():
            # 平台运营进不了租户业务页：应看到 403
            response = page.goto(f"{BASE_URL}/suggestions")
            assert response is not None and response.status == 403, f"期望 403，实际 {response and response.status}"
            return "403"

        check("平台运营访问租户页面被拒（403）", s_isolation)

        browser.close()

    failed = [s for s in steps if not s[1]]
    print(f"\n{len(steps) - len(failed)}/{len(steps)} 通过 · 截图目录 {SHOT_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
