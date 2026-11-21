#!/usr/bin/env bash
# One physical Impression refresh, or restore the picture from before that trial.
set -euo pipefail
action=${1:-}
case "$action" in test|restore) ;; *) echo "Usage: bash scripts/panel-trial.sh test|restore" >&2; exit 2;; esac
if (( EUID == 0 )); then echo "Run as your usual Pi user. The launcher calls sudo for systemd." >&2; exit 2; fi
umask 077
root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
state="$HOME/.local/state/inkypi-panel-trial"
python=/usr/local/inkypi/venv_inkypi/bin/python
live=/usr/local/inkypi/src
unit=inkypi-panel-trial.service
mkdir -p -- "$state"
if [[ "$action" == test && -e "$state/before.png" ]]; then
    echo "A rollback picture already exists. Run restore first." >&2; exit 1
fi
if [[ "$action" == restore && ! -f "$state/before.png" ]]; then
    if [[ -f "$state/last-restored.png" ]] && systemctl is-active --quiet inkypi.service; then
        echo "Already restored. InkyPi is active."; exit 0
    fi
    echo "No pending rollback picture. Check: systemctl is-active inkypi.service" >&2; exit 1
fi
properties=()
if [[ "$action" == test ]]; then
    systemctl is-active --quiet inkypi.service || { echo "The live service must be active to start a trial." >&2; exit 1; }
    "$python" -IB "$root/scripts/panel_trial.py" check "$root/previews/morning.png"
    # systemd lends the unprivileged worker read-only descriptors after stopping
    # the service. A missing file must never prevent ExecStopPost from running.
    properties+=(-p "OpenFile=$live/config/device.json:settings:read-only,graceful"
                 -p "OpenFile=$live/static/images/current_image.png:preview:read-only,graceful")
fi
# The transient service owns the panel even if the SSH client disconnects.
# Only the two fixed systemctl commands run as root.
rc=0
sudo systemd-run --system --wait --collect --unit="$unit" --service-type=exec \
    --uid="$(id -u)" -p UMask=0077 -p RuntimeMaxSec=240 -p TimeoutStopSec=20 \
    -p 'ExecStartPre=+/usr/bin/systemctl stop inkypi.service' \
    -p 'ExecStopPost=+/usr/bin/systemctl start inkypi.service' \
    "${properties[@]}" "$python" -IB "$root/scripts/panel_trial.py" "$action" "$state" || rc=$?
if (( rc != 0 )); then
    echo "Trial command failed. See: sudo journalctl -u $unit -n 30 --no-pager" >&2
    exit "$rc"
fi
systemctl is-active --quiet inkypi.service || { echo "InkyPi did not resume. Run: sudo systemctl start inkypi.service" >&2; exit 1; }
if [[ "$action" == restore ]]; then
    echo "Restore completed. InkyPi is active."
else
    echo "Test refresh completed. InkyPi is active. Undo: bash $root/scripts/panel-trial.sh restore"
fi
