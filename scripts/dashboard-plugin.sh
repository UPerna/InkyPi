#!/usr/bin/env bash
# Enable just Morning Dashboard on an existing InkyPi installation.
set -euo pipefail
action=${1:-}
case "$action" in enable|disable) ;; *) echo "Usage: bash scripts/dashboard-plugin.sh enable|disable" >&2; exit 2;; esac
app=/usr/local/inkypi
name=morning_dashboard
link="$app/src/plugins/$name"
store="$app/dashboard-releases"
root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
rev=$(git -C "$root" rev-parse HEAD)
package="$store/$rev"
unit=inkypi.service
url=http://127.0.0.1
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
# Serialize enable/remove calls across logins, including service readiness checks.
exec 9<"$app"
flock -x 9
systemctl is-active --quiet "$unit" || { echo "Start the installed InkyPi service first." >&2; exit 1; }

ready() {
    local route=$1 marker=$2 attempt
    for ((attempt=0; attempt<60; attempt++)); do
        if curl --noproxy '*' -fs --max-time 3 "$url/$route" -o "$work/page" &&
           grep -Fq -- "$marker" "$work/page"; then return 0; fi
        if systemctl is-failed --quiet "$unit"; then return 1; fi
        sleep 1
    done
    return 1
}

target=$(readlink "$link" || true)
if [[ -L "$link" || -e "$link" ]]; then
    if [[ ! "$target" =~ ^/usr/local/inkypi/dashboard-releases/[0-9a-f]{40}$ ]]; then
        echo "Another installation owns $link. Leaving it in place." >&2; exit 1
    fi
fi
if [[ "$action" == disable ]]; then
    if [[ -z "$target" ]]; then echo "Morning Dashboard is already disabled."; exit 0; fi
    # Do not leave a user's scheduled playlist pointing at a missing plugin.
    sudo /usr/bin/python3 -I - "$app/src/config/device.json" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    lists = json.load(f).get("playlist_config", {}).get("playlists", [])
if any(p.get("plugin_id") == "morning_dashboard" for group in lists for p in group.get("plugins", [])):
    sys.exit("Remove Morning Dashboard from its playlists in the web UI, then run disable again.")
PY
    sudo unlink "$link"
    if sudo systemctl restart "$unit" && ready "" 'class="image-container"'; then
        echo "Morning Dashboard disabled. InkyPi is running."; exit 0
    fi
    sudo ln -sT "$target" "$link"
    sudo systemctl restart "$unit"
    echo "Removal failed its health check. The original plugin link was restored." >&2; exit 1
fi
if [[ -n "$target" ]]; then
    if ready "plugin/$name" 'name="calendarFeeds"'; then
        echo "Morning Dashboard is already enabled at ${target##*/}."; exit 0
    fi
    echo "The existing plugin link is present but its form did not load. Run disable to roll back." >&2; exit 1
fi

# Build from committed files. The helper imports are private to this copy. The
# running installation's HTTP client, Calendar plugin and display driver stay in use.
git -C "$root" archive "$rev" -- "src/plugins/$name" src/plugins/calendar/calendar.py src/utils/http_client.py |
    tar -x -C "$work"
bundle="$work/src/plugins/$name"
cp "$work/src/plugins/calendar/calendar.py" "$bundle/_calendar.py"
cp "$work/src/utils/http_client.py" "$bundle/_http.py"
sed -i 's/^from utils\.http_client import bounded_chunks$/from ._http import bounded_chunks/' \
    "$bundle/$name.py" "$bundle/_calendar.py"
sed -i 's/^from plugins\.calendar\.calendar import Calendar$/from ._calendar import Calendar/' "$bundle/$name.py"
sudo install -d -m 0755 "$store" "$package"
sudo install -m 0644 -- "$bundle"/* "$package/"
sudo ln -sT "$package" "$link"
if sudo systemctl restart "$unit" && ready "plugin/$name" 'name="calendarFeeds"'; then
    echo "Morning Dashboard enabled. Open /plugin/morning_dashboard on your InkyPi."; exit 0
fi
sudo unlink "$link"
sudo systemctl restart "$unit"
if ready "" 'class="image-container"'; then
    echo "The plugin did not load. The previous service is back online." >&2
else
    echo "Rollback removed the plugin link. Check: systemctl status inkypi.service" >&2
fi
exit 1
