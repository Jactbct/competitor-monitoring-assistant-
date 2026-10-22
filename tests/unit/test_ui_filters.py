"""模板过滤器：ISO 字符串要变成人看的格式，而不是原样吐出来。"""

from __future__ import annotations

from datetime import datetime

import pytest

from web.templatetags.ui import human_dt


@pytest.mark.parametrize(
    ("value", "expected_prefix"),
    [
        ("2026-10-23T05:26:00+00:00", "10-23"),
        ("2026-10-23T05:26:00", "10-23"),
        (datetime(2026, 10, 23, 5, 26), "10-23"),
    ],
)
def test_human_dt_formats_iso(value, expected_prefix):
    out = human_dt(value)
    assert out.startswith(expected_prefix)
    assert "T" not in out


def test_human_dt_handles_empty_and_garbage():
    assert human_dt(None) == "—"
    assert human_dt("") == "—"
    assert human_dt("not-a-date") == "not-a-date"[:16]
