"""公开核心的连接级固定偏移时钟；不依赖 IANA tzdata。"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pando.server import _TimezoneClock


UTC_NOW = datetime(2026, 9, 20, 0, 30, tzinfo=timezone.utc)


def test_client_offset_wins_and_zone_name_is_only_metadata():
    clock = _TimezoneClock(
        system_reader=lambda: -60,
        process_reader=lambda: -60,
        utc_now=lambda: UTC_NOW,
        monotonic=lambda: 10.0,
    )
    assert clock.update_client("Asia/Shanghai", 480)
    now = clock.now()
    assert now.utcoffset() == timedelta(hours=8)
    assert (now.hour, now.minute) == (8, 30)


def test_system_failure_falls_back_to_aware_process_time(monkeypatch):
    clock = _TimezoneClock(
        system_reader=lambda: (_ for _ in ()).throw(OSError("registry unavailable")),
        process_reader=lambda: 0,
        utc_now=lambda: UTC_NOW,
        monotonic=lambda: 10.0,
    )
    assert clock.now().tzinfo is not None


def test_invalid_report_clears_client_layer_and_falls_back(caplog):
    clock = _TimezoneClock(
        system_reader=lambda: 0,
        process_reader=lambda: 0,
        utc_now=lambda: UTC_NOW,
        monotonic=lambda: 10.0,
    )
    assert clock.update_client("Asia/Shanghai", 480)
    with caplog.at_level(logging.WARNING, logger="pando"):
        assert not clock.update_client("乱码", 900)
    assert clock.now().utcoffset() == timedelta(0)
    assert "invalid client timezone report" in caplog.text


def test_report_expires_after_24_hours():
    monotonic = [10.0]
    clock = _TimezoneClock(
        system_reader=lambda: -60,
        process_reader=lambda: -60,
        utc_now=lambda: UTC_NOW,
        monotonic=lambda: monotonic[0],
    )
    assert clock.update_client("Asia/Shanghai", 480)
    monotonic[0] += 24 * 60 * 60 + 1
    assert clock.now().utcoffset() == -timedelta(hours=1)


def test_process_staleness_is_observable(caplog):
    clock = _TimezoneClock(
        system_reader=lambda: 480,
        process_reader=lambda: -60,
        utc_now=lambda: UTC_NOW,
        monotonic=lambda: 10.0,
    )
    with caplog.at_level(logging.WARNING, logger="pando"):
        assert clock.system_offset() == 480
    assert "process timezone stale" in caplog.text


def test_frontend_reports_timezone_on_every_connection_and_exposes_shared_reader():
    source = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "offset_minutes:-new Date().getTimezoneOffset()" in source
    assert "{type:'client_timezone',...clientTimezone()}" in source
    assert "clientTimezone," in source.split("window.Pando={", 1)[1]


def test_blank_tz_name_keeps_valid_offset():
    """偏移量是唯一真源：名字缺失/形状不对时只记日志，不作废合法偏移。

    2026-09-24 核账补：部分浏览器环境 Intl 的 timeZone 返回空串，
    原实现会把整条上报作废、退回服务器时区。
    """
    clock = _TimezoneClock(
        system_reader=lambda: -60,
        process_reader=lambda: -60,
        utc_now=lambda: datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc),
        monotonic=lambda: 1000.0,
    )
    for bad_name in ("", None, "Not A Zone", 123):
        assert clock.update_client(bad_name, 480) is True
        assert clock.now().utcoffset() == timedelta(minutes=480), bad_name

    # 偏移本身不合法时仍然降级——上一条不能把这条带松
    assert clock.update_client("Asia/Shanghai", 15 * 60) is False
    assert clock.now().utcoffset() == timedelta(minutes=-60)


def test_frontend_reports_timezone_again_when_page_returns_to_foreground():
    """应用一直开着时改系统时区不会重连——回到前台要比一次偏移并补报。

    2026-09-25 真机发现：改完手机时区必须重启 PWA 才生效，因为上报只发生在建连那一刻。
    """
    source = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "function reportTimezoneIfChanged()" in source
    assert "document.addEventListener('visibilitychange'" in source
    # 只在偏移真的变了时才发，避免每次切前台都打一帧
    assert "if(zone.offset_minutes===_lastReportedTzOffset) return;" in source
