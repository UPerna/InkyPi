"""An Italian morning screen drawn locally, with independent source snapshots."""
from datetime import datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import time as clock
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from plugins.base_plugin.base_plugin import BasePlugin
from plugins.calendar.calendar import Calendar
from plugins.morning_dashboard.agenda import combine_agendas
from plugins.morning_dashboard.cache import read_snapshot, snapshot, snapshot_path
from plugins.morning_dashboard.render import draw_dashboard
from utils.http_client import bounded_chunks


FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_weather(lat, lon, zone):
    fields = "temperature_2m,apparent_temperature,weather_code,surface_pressure,visibility"
    params = {"latitude": lat, "longitude": lon, "timezone": zone.key, "current": fields,
              "daily": "temperature_2m_min,temperature_2m_max", "forecast_days": 2,
              "hourly": "precipitation_probability", "timeformat": "unixtime"}
    deadline = clock.monotonic() + 12
    with requests.get(FORECAST_URL, params=params,
                      timeout=(3, 5), stream=True) as response:
        response.raise_for_status()
        result = json.loads(b"".join(bounded_chunks(response, 64 * 1024, deadline)))
    return parse_weather(result, zone)


def parse_weather(result, zone):
    current, daily = result["current"], result["daily"]
    # Reject incomplete observations before replacing a usable snapshot.
    def number(value):
        if type(value) not in (float, int) or not math.isfinite(value):
            raise ValueError("Incomplete forecast")
        return round(value)

    hourly = result["hourly"] if isinstance(result.get("hourly"), dict) else {}
    times = hourly.get("time") or []
    probabilities = hourly.get("precipitation_probability") or []
    rain = []
    if isinstance(times, list) and isinstance(probabilities, list) and len(times) == len(probabilities):
        for epoch, probability in zip(times, probabilities):
            if type(epoch) is int and type(probability) in (float, int) and 0 <= probability <= 100:
                rain.append([epoch, round(probability)])
    provider_day = datetime.fromtimestamp(daily["time"][0] + result["utc_offset_seconds"], timezone.utc).date()
    return {"at": datetime.fromtimestamp(current["time"], zone).isoformat(timespec="minutes"),
            "temperature": number(current["temperature_2m"]), "code": number(current["weather_code"]),
            "feels": number(current["apparent_temperature"]), "pressure": number(current["surface_pressure"]),
            "visibility_m": None if current.get("visibility") is None else number(current["visibility"]),
            "day": provider_day.isoformat(), "rain": rain,
            "high": number(daily["temperature_2m_max"][0]), "low": number(daily["temperature_2m_min"][0])}


def fetch_agenda(feeds, zone, day):
    # Local midnights can be 23 or 25 hours apart. Cache tomorrow as well for overnight outages.
    until = day + timedelta(days=2)
    start = datetime.combine(day, time.min, zone)
    end = datetime.combine(until, time.min, zone)
    calendar = Calendar({"id": "calendar"})
    events = calendar.fetch_ics_events(feeds, ["#111111"] * len(feeds), zone, start, end,
                                       max_bytes=4 * 1024 * 1024, include_identity=True)
    return {"first": day.isoformat(), "until": until.isoformat(), "events": events}


class MorningDashboard(BasePlugin):
    def generate_image(self, settings, device_config):
        zone = ZoneInfo("Europe/Rome")
        now = datetime.now(zone)
        lat = float(settings.get("latitude", 45.4642))
        lon = float(settings.get("longitude", 9.19))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Coordinate non valide")
        feeds = list(dict.fromkeys(line.strip() for line in settings.get("calendarFeeds", "").splitlines()
                                   if line.strip()))
        if any(urlsplit(url).scheme not in {"webcal", "https", "http"} or not urlsplit(url).hostname
               for url in feeds):
            raise ValueError("URL calendario non valido")
        directory = Path(device_config.config_file).parent / ".morning-cache"
        weather = snapshot(directory, ["weather-v1", lat, lon, zone.key], now,
                           lambda: fetch_weather(lat, lon, zone))
        sources = [snapshot(directory, ["agenda-v1", [feed], zone.key], now,
                            lambda feed=feed: fetch_agenda([feed], zone, now.date()))
                   for feed in feeds]
        agenda = combine_agendas(sources, now)
        # Earlier versions stored all feeds together. Only reuse that exact group
        # when none of its individual feeds can cover today.
        if len(feeds) > 1 and not agenda.covered:
            legacy = read_snapshot(snapshot_path(directory, ["agenda-v1", sorted(feeds), zone.key]), now)
            fallback = combine_agendas([(*legacy, True)], now)
            if fallback.covered:
                fallback.total = fallback.covered = fallback.stale = len(feeds)
                agenda = fallback
        size = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            size = size[::-1]
        return draw_dashboard(size, settings.get("locationName") or "Milano", now, weather, agenda)
