"""采集探针：解析能力的「体检」，也是接新类目时的第一件工具。"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "pages"


def _run(*args) -> str:
    out = StringIO()
    call_command("probe_page", *args, stdout=out)
    return out.getvalue()


def test_probe_fixture_reports_markers_and_price():
    output = _run("--fixture", str(FIXTURES / "1001.html"))

    assert "强结构标记" in output
    assert "/6" in output
    assert "114.00" in output  # 129 - 15 店铺券
    assert "店铺券" in output


def test_probe_flags_unparseable_page_with_exit_code():
    with pytest.raises(SystemExit) as exc:
        _run("--fixture", str(FIXTURES / "structure_changed.html"))
    assert exc.value.code == 1


def test_probe_detects_login_wall():
    """登录墙页同样命中不足强标记，探针会先报异常再以退出码 1 结束。"""
    with pytest.raises(SystemExit) as exc:
        _run("--fixture", str(FIXTURES / "login_wall.html"))
    assert exc.value.code == 1


def test_probe_prints_login_wall_verdict():
    out = StringIO()
    with pytest.raises(SystemExit):
        call_command("probe_page", "--fixture", str(FIXTURES / "login_wall.html"), stdout=out)
    assert "LOGIN_WALL" in out.getvalue()


def test_probe_json_is_machine_readable():
    output = _run("--fixture", str(FIXTURES / "1002.html"), "--json")
    payload = json.loads(output)

    assert payload["structure"]["ok"] is True
    assert payload["price"]["final"] == "79.00"
    assert isinstance(payload["promos"], list)
    assert payload["fetch"]["source"] == "fixture"


def test_probe_requires_input():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        _run()


def test_probe_save_writes_regression_sample(tmp_path, monkeypatch):
    from jobs.management.commands import probe_page as module

    class _Cfg:
        class collector:  # noqa: N801
            fixture_dir = tmp_path

    monkeypatch.setattr(module, "get_settings", lambda: _Cfg())
    _run("--fixture", str(FIXTURES / "1003.html"), "--save")

    saved = list(tmp_path.glob("*.html"))
    assert len(saved) == 1
    assert "高支棉" in saved[0].read_text(encoding="utf-8")
