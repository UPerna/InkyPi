#!/usr/bin/env bash
# Real systemd handoff on a disposable hosted Linux runner. Only the HAT is faked.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]]
live=/usr/local/inkypi
state="$HOME/.local/state/inkypi-panel-trial"
[[ ! -e "$live" && ! -e "$state" ]]
[[ $(systemctl show inkypi.service -p LoadState --value) == not-found ]]
tmp=$(mktemp -d)
mkdir -p "$tmp/installed" "$state" previews
git archive 20994119e38b891be0cb6579335a3364dd32982c src | tar -x -C "$tmp/installed"
sudo mkdir "$live"
sudo ln -s "$tmp/installed/src" "$live/src"
python -m venv --system-site-packages "$tmp/venv"
sudo ln -s "$tmp/venv" "$live/venv_inkypi"
site=$("$tmp/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
mkdir "$site/inky"
touch "$site/inky/__init__.py"
cat > "$site/inky/auto.py" <<'PY'
from pathlib import Path
import os
import subprocess
import sys
class Inky:
    __module__ = "inky.inky_e673"
    width, height, BLACK = 800, 480, 0
    def set_border(self, colour):
        pass
    def set_image(self, frame, saturation=0.5):
        self.frame = frame
        assert saturation == 0.7
    def show(self):
        assert os.getuid() != 0
        assert subprocess.check_output(["systemctl", "show", "-p", "ActiveState", "--value",
                                        "inkypi.service"], text=True).strip() == "inactive"
        state = Path.home() / ".local/state/inkypi-panel-trial"
        if (state / "fail-next").exists():
            (state / "fail-next").unlink()
            raise OSError("simulated transfer failure")
        self.frame.save(state / "simulated.png")
        with (state / "calls").open("a") as f:
            print(Path(sys._getframe(1).f_globals["__file__"]).resolve(), file=f)
def auto():
    return Inky()
PY
printf '[Service]\nType=simple\nExecStart=/usr/bin/sleep infinity\n' | sudo tee /run/systemd/system/inkypi.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl start inkypi.service
trap 'sudo journalctl -u inkypi-panel-trial.service --no-pager -n 35' ERR
src="$live/src"
printf '%s\n' '{"resolution":[800,480],"orientation":"vertical","image_settings":{"inky_saturation":0.7}}' > "$src/config/device.json"
mkdir -p "$src/static/images"
python - "$src/static/images/current_image.png" <<'PY'
from PIL import Image
import sys
img = Image.new("RGB", (480, 800), "white")
img.putpixel((2, 3), (0, 0, 0))
img.save(sys.argv[1])
Image.new("RGB", (800, 480), "blue").save("previews/morning.png")
PY
sudo chown root:root "$src/config/device.json" "$src/static/images/current_image.png"
sudo chmod 600 "$src/config/device.json" "$src/static/images/current_image.png"
bash scripts/panel-trial.sh test
cmp <(realpath src/display/inky_display.py) "$state/calls"
[[ -s "$state/before.png" ]] && systemctl is-active --quiet inkypi
if bash scripts/panel-trial.sh test; then exit 1; fi
touch "$state/fail-next"
if bash scripts/panel-trial.sh restore; then exit 1; fi
[[ -s "$state/before.png" ]] && systemctl is-active --quiet inkypi
bash scripts/panel-trial.sh restore
bash scripts/panel-trial.sh restore
[[ $(wc -l < "$state/calls") -eq 2 && ! -e "$state/before.png" ]]
[[ $(tail -1 "$state/calls") == "$(realpath "$src/display/inky_display.py")" ]]
python - "$state" <<'PY'
from PIL import Image
from pathlib import Path
import sys
state = Path(sys.argv[1])
with Image.open(state / "simulated.png") as frame, Image.open(state / "last-restored.png") as saved:
    assert frame.size == (800, 480) and frame.getpixel((3, 477)) == (0, 0, 0)
    assert frame.getpixel((2, 3)) == (255, 255, 255) and frame.tobytes() == saved.tobytes()
    assert saved.info["inkypi.saturation"] == "0.7"
PY
# A current-format install saves the oriented frame. It must not be rotated again.
sudo ln -sfn "$PWD/src" "$live/src"
cp "$state/last-restored.png" src/static/images/current_image.png
sudo cp "$tmp/installed/src/config/device.json" src/config/device.json
touch "$state/fail-next"
if bash scripts/panel-trial.sh test; then exit 1; fi
systemctl is-active --quiet inkypi
bash scripts/panel-trial.sh restore
python - "$state" <<'PY'
from PIL import Image
from pathlib import Path
import sys
with Image.open(Path(sys.argv[1]) / "simulated.png") as frame:
    assert frame.size == (800, 480) and frame.getpixel((3, 477)) == (0, 0, 0)
PY
