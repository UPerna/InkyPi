from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import icalendar
import pytest
from plugins.calendar.calendar import Calendar
from plugins.morning_dashboard import morning_dashboard as dashboard
from plugins.morning_dashboard.cache import snapshot
from plugins.morning_dashboard.render import agenda_rows, freshness
from utils import image_utils

ROME = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 25, 9, 30, tzinfo=ROME)


def test_recurrences_midnight_floating_and_dst(monkeypatch):
    fixture = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//QC//EN
BEGIN:VEVENT
UID:everyday
DTSTART;TZID=Europe/Rome:20261024T090000
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Colazione
END:VEVENT
BEGIN:VEVENT
UID:family
DTSTART;VALUE=DATE:20261024
DTEND;VALUE=DATE:20261026
SUMMARY:Famiglia
END:VEVENT
BEGIN:VEVENT
UID:late
DTSTART:20261024T210000Z
DTEND:20261024T220000Z
SUMMARY:Finito ieri
END:VEVENT
BEGIN:VEVENT
UID:floating
DTSTART:20261025T123000
SUMMARY:Pranzo
END:VEVENT
END:VCALENDAR"""
    monkeypatch.setattr(Calendar, "fetch_calendar", lambda self, url, **kw: icalendar.Calendar.from_ical(fixture))
    data = dashboard.fetch_agenda(["https://example.invalid/1"], ROME, NOW.date())
    assert agenda_rows(data, NOW) == [("Tutto il giorno", "Famiglia"), ("09:00", "Colazione"), ("12:30", "Pranzo")]
    assert agenda_rows(data, NOW + timedelta(days=1)) == [("09:00", "Colazione")]
    assert agenda_rows(data, NOW + timedelta(days=2)) is None


def fail():
    raise OSError("offline")


def test_cache_persistence_isolation_corruption_and_recovery(tmp_path):
    source = ["agenda", "https://example.invalid/private-test-feed"]
    data = {"events": []}
    assert snapshot(tmp_path, source, NOW, fail) == (None, None, True)
    assert snapshot(tmp_path, source, NOW, lambda: data) == (data, NOW, False)
    assert snapshot(tmp_path, source, NOW + timedelta(minutes=2), fail) == (data, NOW, False)
    later = NOW + timedelta(minutes=16)
    assert snapshot(tmp_path, source, later, fail) == (data, NOW, True)
    assert snapshot(tmp_path, ["agenda", "other"], later, fail) == (None, None, True)
    assert snapshot(tmp_path, source, later, lambda: {"events": [1]}) == ({"events": [1]}, later, False)
    path, = tmp_path.iterdir()
    assert "private-test-feed" not in str(path) + path.read_text()
    path.write_text("{")
    assert snapshot(tmp_path, source, later, fail) == (None, None, True)
    assert "25/10/2026 09:30" in freshness(NOW, True)


def test_dashboard_entry_point_restarts_outage_and_recovers(tmp_path, monkeypatch):
    current = [NOW]
    class Frozen(datetime):
        @classmethod
        def now(cls, zone):
            return current[0].astimezone(zone)

    monkeypatch.setattr(dashboard, "datetime", Frozen)
    calls = {"weather": 0, "agenda": 0}
    unavailable = set()
    def meteo(*args):
        calls["weather"] += 1
        if "weather" in unavailable:
            return fail()
        return {"at": "2026-10-25T09:15", "temperature": 12, "code": 0, "feels": 11, "pressure": 1013,
                "visibility_m": 20000, "day": "2026-10-25", "high": 16, "low": 8}
    def agenda(*args):
        calls["agenda"] += 1
        if "agenda" in unavailable:
            return fail()
        return {"first": "2026-10-25", "until": "2026-10-27", "events": []}
    monkeypatch.setattr(dashboard, "fetch_weather", meteo)
    monkeypatch.setattr(dashboard, "fetch_agenda", agenda)
    monkeypatch.setattr(image_utils.subprocess, "Popen", lambda *a, **k: pytest.fail("browser launched"))
    conf = SimpleNamespace(config_file=tmp_path/"device.json", get_resolution=lambda: (800, 480),
                           get_config=lambda key: "horizontal")
    def render():
        plugin = dashboard.MorningDashboard({"id": "morning_dashboard"})
        return plugin.generate_image({"calendarFeeds": "https://example.invalid/family"}, conf)
    fresh = render()
    assert fresh.size == (800, 480) and fresh.mode == "RGB"
    assert fresh.tobytes() == render().tobytes()
    assert calls == {"weather": 1, "agenda": 1}
    current[0] += timedelta(minutes=16)
    unavailable.add("weather")
    partial = render()
    assert partial.tobytes() != fresh.tobytes()
    stamps = [json.loads(path.read_text()) for path in (tmp_path/".morning-cache").glob("*.json")]
    assert {p["at"] for p in stamps} == {NOW.isoformat(), current[0].isoformat()}
    unavailable.add("agenda")
    current[0] += timedelta(minutes=16)
    frozen = render().tobytes()
    assert frozen == render().tobytes()
    unavailable.clear()
    assert render().tobytes() != frozen
    conf.get_config = lambda key: "vertical"
    assert render().size == (480, 800)


def test_firefox_missing_does_not_start_chromium(monkeypatch):
    monkeypatch.setattr(image_utils.shutil, "which", lambda name: None)
    monkeypatch.setattr(image_utils.subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected fallback"))
    assert image_utils.take_screenshot("about:blank", (800, 480), engine="firefox") is None
