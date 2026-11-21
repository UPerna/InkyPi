#!/usr/bin/env bash
# A disposable system service running the original application with its mock display.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]]
app=/usr/local/inkypi
unit=inkypi.service
[[ ! -e "$app" && $(systemctl show "$unit" -p LoadState --value) == not-found ]]
scratch=$(mktemp -d)
git archive 20994119e38b891be0cb6579335a3364dd32982c src | tar -x -C "$scratch"
sudo mkdir "$app"
sudo ln -s "$scratch/src" "$app/src"
src="$app/src"
link="$src/plugins/morning_dashboard"
cp "$src/config/device_dev.json" "$src/config/device.json"
py=$(command -v python)
cat <<UNIT | sudo tee "/run/systemd/system/$unit" >/dev/null
[Service]
WorkingDirectory=$scratch
ExecStart=$py $src/inkypi.py
UNIT
sudo systemctl daemon-reload
sudo systemctl start "$unit"
trap 'sudo journalctl -u "$unit" --no-pager -n 45' ERR
base=http://127.0.0.1
curl --retry 25 --retry-delay 1 --retry-connrefused -fsS "$base/plugin/clock" -o /dev/null
[[ $(curl -s -o /dev/null -w '%{http_code}' "$base/plugin/morning_dashboard") == 404 ]]
shared=("$src/plugins/calendar/calendar.py" "$src/utils/http_client.py" "$src/display/inky_display.py")
sha256sum "${shared[@]}" > "$scratch/original.sha256"
bash scripts/dashboard-plugin.sh enable
pid=$(systemctl show "$unit" -p MainPID --value)
bash scripts/dashboard-plugin.sh enable
[[ $(systemctl show "$unit" -p MainPID --value) == "$pid" ]]
curl -fsS "$base/plugin/calendar" -o /dev/null

# Exercise Update Now with a fresh local iCal feed and a synthetic weather snapshot.
sudo env PYTHONPATH="$src" "$py" - "$src" <<'PY'
from datetime import datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import sys
from zoneinfo import ZoneInfo

from PIL import Image
import requests
from plugins.morning_dashboard.cache import snapshot, snapshot_path

directory = Path(sys.argv[1]) / "config" / ".morning-cache"
now = datetime.now(ZoneInfo("Europe/Rome"))
weather = dict(temperature=6, feels=4, pressure=1010, code=0, low=3, high=9,
               day=now.date().isoformat(), at=now.isoformat(), rain=[])
snapshot(directory, ["weather-v1", 45.4642, 9.19, "Europe/Rome"], now, lambda: weather)
with TemporaryDirectory() as feeds:
    calendar = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//InkyPi//QC//EN\r\nBEGIN:VEVENT\r\n"
                f"UID:test-display\r\nDTSTART;VALUE=DATE:{now:%Y%m%d}\r\n"
                f"DTEND;VALUE=DATE:{now + timedelta(days=1):%Y%m%d}\r\n"
                "SUMMARY:Calendar check\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    (Path(feeds) / "test.ics").write_bytes(calendar.encode())
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=feeds)) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        feed = f"http://127.0.0.1:{server.server_port}/test.ics"
        response = requests.post("http://127.0.0.1/update_now",
                                 data={"plugin_id": "morning_dashboard", "calendarFeeds": feed}, timeout=25)
        response.raise_for_status()
        assert response.json()["success"]
        server.shutdown()
    saved = json.loads(snapshot_path(directory, ["agenda-v1", [feed], "Europe/Rome"]).read_text())
    assert saved["data"]["events"][0]["uid"] == "test-display"
response = requests.get("http://127.0.0.1/api/current_image", timeout=5)
response.raise_for_status()
with Image.open(BytesIO(response.content)) as frame:
    assert frame.format == "PNG" and frame.size == (800, 480)
    assert frame.convert("L").getextrema() == (0, 255)
PY

config="$src/config/device.json"
sudo cp "$config" "$scratch/before-playlist.json"
sudo "$py" - "$config" <<'PY'
import json, sys
from pathlib import Path
config = Path(sys.argv[1])
data = json.loads(config.read_text())
data["playlist_config"]["playlists"][0]["plugins"].append({"plugin_id": "morning_dashboard"})
config.write_text(json.dumps(data))
PY
if bash scripts/dashboard-plugin.sh disable; then exit 1; fi
[[ -L "$link" && $(systemctl show "$unit" -p MainPID --value) == "$pid" ]]
sudo cp "$scratch/before-playlist.json" "$config"
bash scripts/dashboard-plugin.sh disable
bash scripts/dashboard-plugin.sh disable
[[ ! -L "$link" && $(curl -s -o /dev/null -w '%{http_code}' "$base/plugin/morning_dashboard") == 404 ]]

# Fail the service start only when the candidate link exists.
sudo mkdir "/run/systemd/system/$unit.d"
printf '[Service]\nExecStartPre=/usr/bin/test ! -L %s\n' "$link" |
    sudo tee "/run/systemd/system/$unit.d/test.conf" >/dev/null
sudo systemctl daemon-reload
if bash scripts/dashboard-plugin.sh enable; then exit 1; fi
[[ ! -L "$link" ]]
systemctl is-active --quiet "$unit"
curl -fsS "$base/plugin/clock" -o /dev/null
sha256sum -c "$scratch/original.sha256"
