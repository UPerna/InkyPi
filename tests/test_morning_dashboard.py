from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import icalendar
import pytest
from plugins.calendar.calendar import Calendar
from plugins.morning_dashboard import morning_dashboard as dashboard
from plugins.morning_dashboard.agenda import combine_agendas
from plugins.morning_dashboard.cache import snapshot, snapshot_path
from plugins.morning_dashboard.render import freshness, hourly_outlook
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
    items = combine_agendas([(data, NOW, False)], NOW).items
    assert [(e.label, e.title) for e in items] == [("12:30", "Pranzo"), ("Tutto il giorno", "Famiglia"),
                                                   ("09:00", "Colazione")]
    tomorrow = combine_agendas([(data, NOW, True)], NOW + timedelta(days=1))
    assert [e.title for e in tomorrow.items] == ["Colazione"]
    assert not combine_agendas([(data, NOW, True)], NOW + timedelta(days=2)).covered


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


def test_individual_feeds_and_legacy_offline_upgrade(tmp_path, monkeypatch):
    a, b = "https://a.invalid/private", "https://b.invalid/private"
    conf = SimpleNamespace(config_file=tmp_path/"device.json", get_resolution=lambda: (800, 480),
                           get_config=lambda key: "horizontal")
    monkeypatch.setattr(dashboard, "draw_dashboard", lambda size, location, now, weather, agenda: agenda)
    monkeypatch.setattr(dashboard, "fetch_weather", lambda *args: fail())
    class Frozen(datetime):
        @classmethod
        def now(cls, zone):
            return NOW
    monkeypatch.setattr(dashboard, "datetime", Frozen)
    directory = tmp_path/".morning-cache"
    def payload(title):
        return {"first": "2026-10-25", "until": "2026-10-27", "events": [
            {"start": "2026-10-25", "allDay": True, "title": title}]}
    old = NOW - timedelta(minutes=19)
    snapshot(directory, ["agenda-v1", [a, b], ROME.key], old, lambda: payload("Vecchio"))
    healthy = {}
    def fetch(feeds, zone, day):
        assert len(feeds) == 1
        if feeds[0] not in healthy:
            return fail()
        return payload(healthy[feeds[0]])
    monkeypatch.setattr(dashboard, "fetch_agenda", fetch)
    plugin = dashboard.MorningDashboard({"id": "morning_dashboard"})
    def run(feeds):
        return plugin.generate_image({"calendarFeeds": "\n".join(feeds)}, conf)
    result = run([b, a])
    assert (result.total, result.covered, result.stale, result.oldest) == (2, 2, 2, old)
    assert [e.title for e in result.items] == ["Vecchio"]
    assert not run([a]).covered
    healthy[a] = "Nuovo"
    result = run([a, b])
    assert (result.covered, result.total) == (1, 2)
    assert [e.title for e in result.items] == ["Nuovo"]
    healthy[b] = "Scuola"
    assert [e.title for e in run([a, b]).items] == ["Nuovo", "Scuola"]
    assert [e.title for e in run([b]).items] == ["Scuola"]
    assert a not in snapshot_path(directory, ["agenda-v1", [a], ROME.key]).read_text()


def test_shared_invites_preserve_distinct_events_and_prioritise_active():
    def event(uid, start, end, title):
        return {"uid": uid, "title": title, "allDay": False, "start": "2026-10-25T" + start + "+01:00",
                "end": "2026-10-25T" + end + "+01:00"}
    shared = event("one", "10:00", "11:00", "Nuovo")
    def data(*events):
        return {"first": "2026-10-25", "until": "2026-10-27", "events": list(events)}
    fresh = data(shared, event("two", "10:00", "11:00", "Nuovo"), event("run", "09:00", "09:45", "Attivo"),
                 event("past", "08:30", "09:30", "Finito"), event("later", "14:00", "15:00", "Più tardi"))
    saved = data(dict(shared, title="Prima"))
    broken = data({"title": "Corrotto"})
    merged = combine_agendas([(fresh, NOW, False), (saved, NOW-timedelta(hours=2), True), (broken, NOW, False)], NOW)
    assert [i.title for i in merged.items] == ["Attivo", "Nuovo", "Nuovo", "Più tardi", "Finito"]
    assert (merged.covered, merged.total, merged.stale) == (2, 3, 1)


def test_rescheduled_invite_and_distinct_recurrence():
    original = {"uid": "invite", "start": "2026-10-25T10:00:00+01:00", "allDay": False, "title": "Prima"}
    moved = dict(original, start="2026-10-25T13:00:00+01:00",
                 occurrence="2026-10-25T09:00:00+00:00", title="Spostato")
    separate = dict(original, start="2026-10-25T16:00:00+01:00", title="Seconda ricorrenza")
    def data(events):
        return {"first": "2026-10-25", "until": "2026-10-27", "events": events}
    merged = combine_agendas([(data([original]), NOW-timedelta(hours=1), True),
                              (data([moved, separate]), NOW, False)], NOW)
    assert [(i.label, i.title) for i in merged.items] == [("13:00", "Spostato"), ("16:00", "Seconda ricorrenza")]
    moved["start"] = "2026-10-26T13:00:00+01:00"
    merged = combine_agendas([(data([moved]), NOW, False), (data([original]), NOW-timedelta(hours=1), True)], NOW)
    assert merged.covered == 2 and not merged.items


@pytest.mark.parametrize("start, expected", [
    (datetime(2026, 3, 29, 1, 30, tzinfo=ROME), ["01:00", "03:00", "04:00"]),
    (datetime(2026, 10, 25, 1, 30, tzinfo=ROME), ["01:00", "02 CEST", "02 CET"]),
    (datetime(2026, 11, 21, 23, 55, tzinfo=ROME), ["23:00", "00:00", "01:00"]),
])
def test_outlook_interval_end_clock_changes_and_missing_samples(start, expected):
    hour = int(start.timestamp()) // 3600 * 3600
    result = hourly_outlook({"rain": [[hour, 99], [hour+3600, 0], [hour+10800, 80]]}, start)
    assert [label for label, prob in result[:3]] == expected
    assert [prob for label, prob in result] == [0, None, 80, None, None, None]


def test_weather_decoding_keeps_current_conditions_on_partial_hourly_data():
    epoch = int(NOW.timestamp())
    payload = {"current": {"time": epoch, "temperature_2m": 12, "apparent_temperature": 9, "weather_code": 2,
                            "surface_pressure": 1001}, "utc_offset_seconds": 3600,
               "daily": {"time": [1792882800], "temperature_2m_min": [7], "temperature_2m_max": [16]},
               "hourly": {"time": [epoch + i * 3600 for i in range(5)],
                          "precipitation_probability": [0, None, -2, 100, 101]}}
    parsed = dashboard.parse_weather(payload, ROME)
    assert parsed["rain"] == [[epoch, 0], [epoch + 10800, 100]]
    assert datetime.fromisoformat(parsed["at"]) == NOW
    assert parsed["day"] == "2026-10-25"
    payload["hourly"]["time"].pop()
    assert dashboard.parse_weather(payload, ROME)["rain"] == []
