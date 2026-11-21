"""Measure a disposable, loopback-only InkyPi service with its real refresh worker."""
import argparse
from datetime import datetime, timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import time
from zoneinfo import ZoneInfo

from PIL import Image
import psutil
import requests

from pi_memory import measure_scope, new_scope, scope_command


def serve(root):
    from waitress import create_server
    sys.path.insert(0, str(root / "src"))
    sys.argv = ["inkypi.py", "--dev"]
    import inkypi

    server = create_server(inkypi.app, host="127.0.0.1", port=0, threads=1)
    if (root / "with-dashboard").exists():
        # Fixtures live only inside this disposable service. Production URLs and
        # user configuration are never read. Import plugins at first refresh.
        from flask import request

        class FixtureClock(datetime):
            @classmethod
            def now(cls, zone):
                return datetime.fromisoformat((root / "fixture-time").read_text()).astimezone(zone)

        @inkypi.app.before_request
        def use_local_fixture():
            if request.path == "/update_now" and request.form.get("plugin_id") == "morning_dashboard":
                from plugins.morning_dashboard import morning_dashboard
                morning_dashboard.FORECAST_URL = f"http://127.0.0.1:{server.effective_port}/qc/weather"
                morning_dashboard.datetime = FixtureClock

        @inkypi.app.get("/qc/weather")
        def weather():
            if (root / "weather-offline").exists():
                return "Offline fixture", 503
            now = FixtureClock.now(ZoneInfo("Europe/Rome"))
            assert request.args["timeformat"] == "unixtime" and request.args["hourly"] == "precipitation_probability"
            midnight = int(now.replace(hour=0, minute=0).timestamp())
            return {"current": {"time": int(now.timestamp()), "temperature_2m": 13,
                                "apparent_temperature": 12, "surface_pressure": 1012,
                                "weather_code": 2, "visibility": 12000},
                    "utc_offset_seconds": int(now.utcoffset().total_seconds()),
                    "daily": {"time": [midnight], "temperature_2m_max": [18], "temperature_2m_min": [8]},
                    "hourly": {"time": [midnight + i * 3600 for i in range(24)],
                               "precipitation_probability": [0] * 9 + [10, 25, 65, 80, 55, 20] + [0] * 9}}

        @inkypi.app.get("/qc/calendar/<name>")
        def agenda(name):
            if name not in {"family", "school"}:
                return "Unknown fixture", 404
            if (root / (name + "-offline")).exists():
                return "Offline fixture", 503
            day = FixtureClock.now(ZoneInfo("Europe/Rome")).strftime("%Y%m%d")
            entries = [("shared", "093000", "Biblioteca di quartiere")]
            entries += ([("family", "073000", "Piscina in famiglia"), ("market", "110000", "Mercato")]
                        if name == "family" else [("school", "140000", "Laboratorio di musica")])
            events = "".join(f"BEGIN:VEVENT\r\nUID:{uid}@localhost\r\nDTSTAMP:{day}T060000Z\r\n"
                             f"DTSTART:{day}T{hour}Z\r\nSUMMARY:{title}\r\nEND:VEVENT\r\n"
                             for uid, hour, title in entries)
            return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//InkyPi//QC//EN\r\n" + events + "END:VCALENDAR\r\n"

        def prevent_dashboard_subprocess(event, values):
            if event == "subprocess.Popen" and (root / "dashboard-active").exists():
                raise RuntimeError("Dashboard attempted to launch a subprocess")
        sys.addaudithook(prevent_dashboard_subprocess)
    inkypi.refresh_task.start()
    (root / "service-pid").write_text(str(os.getpid()))
    (root / "port.tmp").write_text(str(server.effective_port))
    (root / "port.tmp").replace(root / "port")
    try:
        server.run()
    finally:
        inkypi.refresh_task.stop()


def memory(service):
    rss = pss = processes = 0
    for child in [service, *service.children(recursive=True)]:
        try:
            info = child.memory_full_info()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        rss += info.rss
        pss += info.pss
        processes += 1
    return {"rss_mib": round(rss / 2**20, 1), "pss_mib": round(pss / 2**20, 1),
            "processes": processes}


def refresh(session, url, service, form, marker=None):
    start = time.monotonic()
    response = session.post(url + "/update_now", data=form, timeout=5)
    assert response.status_code == 202, response.text
    peak = memory(service)
    while time.monotonic() - start < 120:
        current = memory(service)
        peak = {key: max(peak[key], value) for key, value in current.items()}
        result = session.get(url + response.json()["status_url"], timeout=5).json()
        if result["state"] == "completed":
            png = session.get(url + "/api/current_image", timeout=5)
            png.raise_for_status()
            with Image.open(BytesIO(png.content)) as frame:
                assert frame.size == (800, 480)
                assert frame.convert("L").getextrema() == (0, 255)
                if marker is not None:
                    assert frame.convert("RGB").getpixel((740, 430)) == marker, "JavaScript marker missing"
            return {"sampled_peak": peak, "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
                    "displayed": result["message"] == "Display updated."}
        if result["state"] != "running" and result["state"] != "queued":
            raise RuntimeError(result["message"])
        time.sleep(0.03)
    raise TimeoutError("Service refresh did not finish")


def exercise_dashboard(root, session, url, service, preview_dir):
    (root / "dashboard-active").touch()
    form = {"plugin_id": "morning_dashboard", "calendarFeeds": "\n".join(
            url + "/qc/calendar/" + name for name in ("family", "school"))}
    clock = datetime(2026, 11, 21, 8, tzinfo=ZoneInfo("Europe/Rome"))
    # Elapsed minutes, unavailable sources and expected last successful reads.
    phases = ((0, set(), (0, 0, 0)), (16, {"weather", "school"}, (0, 16, 0)),
              (32, {"weather", "family"}, (0, 16, 32)), (33, {"family"}, (33, 16, 32)),
              (34, set(), (33, 34, 32)))
    peaks = []
    for minutes, offline, expected in phases:
        fixture_time = clock + timedelta(minutes=minutes)
        (root / "fixture-time").write_text(fixture_time.isoformat())
        for name in ("weather", "family", "school"):
            marker = root / (name + "-offline")
            if name in offline:
                marker.touch()
            else:
                marker.unlink(missing_ok=True)
        for index in range(20):
            sample = refresh(session, url, service, form)
            assert sample["displayed"] == (index == 0), (minutes, index, sample)
            peaks.append(sample["sampled_peak"])
        snapshots = [json.loads(p.read_text(encoding="utf-8"))
                     for p in (root / "src/config/.morning-cache").glob("*.json")]
        weather, = [s for s in snapshots if s["data"].get("temperature") == 13]
        assert len(weather["data"]["rain"]) == 24
        observed = [weather]
        for uid in ("family@localhost", "school@localhost"):
            snapshot, = [s for s in snapshots if any(e.get("uid") == uid for e in s["data"].get("events", []))]
            observed.append(snapshot)
        assert [datetime.fromisoformat(s["at"]) for s in observed] == [clock + timedelta(minutes=m) for m in expected]
        if preview_dir and minutes in (0, 32):
            preview_dir.mkdir(parents=True, exist_ok=True)
            png = session.get(url + "/api/current_image", timeout=5)
            png.raise_for_status()
            (preview_dir / ("morning.png" if minutes == 0 else "partial-outage.png")).write_bytes(png.content)
    return {"refreshes": len(peaks), "display_updates": len(phases), "unchanged": len(peaks) - len(phases),
            "sampled_peak": {k: max(p[k] for p in peaks) for k in peaks[0]}}


def run_checkout(checkout, with_browser, dashboard_runs=False, pi_budget=False, preview_dir=None):
    with tempfile.TemporaryDirectory(prefix="inkypi-service-qc-") as temporary:
        root = Path(temporary)
        archive = root / "src.tar"
        subprocess.run(["git", "-C", str(checkout), "archive", "-o", str(archive), "HEAD", "src"], check=True)
        with tarfile.open(archive) as stream:
            stream.extractall(root, filter="data")
        page = root / "card.html"
        if dashboard_runs:
            (root / "with-dashboard").touch()
        page.write_text("""<style>body{font:30px sans-serif} i{position:absolute;left:730px;top:420px;width:25px;height:25px}</style>
            <p>Milano | 07:35</p><p>09:00 Agenda | Meteo 12 C</p><i></i>
            <script>document.querySelector('i').style.background=location.hash=='#firefox'?'blue':'red'</script>""")
        env = dict(os.environ, SRC_DIR=str(root / "src"), PYTHONPATH=str(root / "src"))
        with (root / "service.log").open("w") as log, requests.Session() as session:
            session.trust_env = False
            start = time.monotonic()
            command = [sys.executable, __file__, "--serve", str(root)]
            if pi_budget:
                unit = new_scope()
                command = scope_command(unit, command)
            with subprocess.Popen(command, cwd=root,
                                  env=env, stdout=log, stderr=subprocess.STDOUT) as child:
                try:
                    while not (root / "port").exists():
                        if child.poll() is not None or time.monotonic() - start > 60:
                            raise RuntimeError("Mock service did not start")
                        time.sleep(0.02)
                    url = "http://127.0.0.1:" + (root / "port").read_text()
                    service = psutil.Process(int((root / "service-pid").read_text()))
                    result = {"architecture": platform.machine(), "startup_ms": round((time.monotonic() - start) * 1000, 1),
                              "idle": memory(service)}
                    result["clock"] = refresh(session, url, service, {"plugin_id": "clock", "selectedClockFace": "Digital Clock"})
                    if with_browser:
                        for engine, color in (("chromium", (255, 0, 0)), ("firefox", (0, 0, 255))):
                            form = {"plugin_id": "screenshot", "url": page.as_uri() + "#" + engine,
                                    "browserEngine": engine}
                            result[engine] = refresh(session, url, service, form, marker=color)
                    if dashboard_runs:
                        result["dashboard"] = exercise_dashboard(root, session, url, service, preview_dir)
                        if pi_budget:
                            result["dashboard"]["cgroup"] = measure_scope(service.pid, unit)
                    return result
                except Exception:
                    print((root / "service.log").read_text()[-1800:], file=sys.stderr)
                    raise
                finally:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--dashboard-only", action="store_true",
                        help="run 100 dashboard refreshes in the disposable service without starting a browser")
    parser.add_argument("--pi-budget", action="store_true",
                        help="measure the mock service in a fresh 200 MiB systemd user scope, with a 160 MiB peak gate")
    parser.add_argument("--preview-dir", type=Path, help="save synthetic healthy and partial-outage screen PNGs")
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
    else:
        report = {"candidate": run_checkout(Path(__file__).resolve().parents[1],
                                             not (args.dashboard_only or args.pi_budget), True, args.pi_budget,
                                             args.preview_dir)}
        if args.baseline:
            report["baseline"] = run_checkout(args.baseline, False)
        print(json.dumps(report, indent=2))
        if args.pi_budget and report["candidate"]["dashboard"]["cgroup"]["status"] != "passed":
            sys.exit(1)
