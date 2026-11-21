"""Worker for panel-trial.sh. All image and driver work runs as the Pi user."""
import argparse
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic

from PIL import Image, PngImagePlugin


LIVE = Path("/usr/local/inkypi/src")
ROOT = Path(__file__).resolve().parents[1]
SIZE = (800, 480)
PANELS = {"inky.inky_ac073tc1a", "inky.inky_e673"}


def trial_image(path):
    with Image.open(path) as source:
        if source.format != "PNG" or source.size != SIZE:
            raise ValueError("Expected an 800 × 480 PNG. Keep the saved dashboard preview in previews/morning.png.")
        return source.copy()


def opened_file(name, path):
    # Confirm the descriptor is for the file left behind after the service stopped.
    # A replaced file or missing descriptor aborts before the panel is touched.
    names = os.environ.get("LISTEN_FDNAMES", "").split(":")
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or name not in names:
        raise RuntimeError(f"Could not open the installed {name} file for the rollback checkpoint.")
    fd = 3 + names.index(name)
    if not os.path.samestat(os.fstat(fd), path.stat()):
        raise RuntimeError(f"The installed {name} file changed while the service was stopping.")
    return os.fdopen(os.dup(fd), "rb")


def saved_frame(source, config, live):
    # Deployed releases save either the source PNG or the final transformed
    # frame. Apply the installed implementation's transforms exactly once.
    from display.display_manager import DisplayManager
    if hasattr(DisplayManager, "prepare_frame"):
        return source
    from utils.image_utils import apply_image_enhancement, change_orientation, compute_image_hash, resize_image
    last = config.get("refresh_info") or {}
    if last.get("image_hash") and compute_image_hash(source) != last["image_hash"]:
        raise ValueError("The saved image disagrees with the last recorded refresh. Trial cancelled.")
    oriented = change_orientation(source, config.get("orientation") or "horizontal")
    options = []
    if oriented.width * SIZE[1] != oriented.height * SIZE[0]:
        plugin = last.get("plugin_id")
        if not isinstance(plugin, str) or not plugin or Path(plugin).name != plugin or plugin in {".", ".."}:
            raise ValueError("Cannot determine the original plugin crop for this image.")
        info = json.loads((live / "plugins" / plugin / "plugin-info.json").read_text(encoding="utf-8"))
        options = info.get("image_settings", [])
    frame = resize_image(oriented, SIZE, options)
    if config.get("inverted_image"):
        frame = frame.rotate(180)
    return apply_image_enhancement(frame, config.get("image_settings") or {})


def save_checkpoint(state, frame, saturation, model):
    if frame.size != SIZE:
        raise ValueError("The installed preview does not match this panel.")
    meta = PngImagePlugin.PngInfo()
    meta.add_text("inkypi.panel", model)
    meta.add_text("inkypi.saturation", str(saturation))
    fd, name = tempfile.mkstemp(dir=state)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as tmp:
            frame.save(tmp, format="PNG", pnginfo=meta)
            tmp.flush()
            os.fsync(tmp.fileno())
        # Even a repeat test may never replace an outstanding recovery image.
        os.link(path, state / "before.png")
        dirfd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        path.unlink(missing_ok=True)


class TrialConfig:
    def __init__(self, saturation):
        self.values = {"resolution": list(SIZE), "image_settings": {"inky_saturation": saturation}}

    def get_config(self, key, default=None):
        return self.values.get(key, default)

    def update_value(self, *args, **kwargs):
        raise ValueError("Detected an unexpected panel size. Trial cancelled.")


def adapter(saturation):
    from display.inky_display import InkyDisplay
    display = InkyDisplay(TrialConfig(saturation))
    chip = display.inky_display
    model = type(chip).__module__
    if model not in PANELS or (chip.width, chip.height) != SIZE:
        raise ValueError("Expected a Pimoroni Impression 7.3-inch AC073TC1A or E673.")
    return display, model


def worker(action, state):
    before = state / "before.png"
    if action == "draw":
        # A fresh interpreter imports only the candidate adapter. Both releases
        # use the existing installed vendor library and the same physical HAT.
        sys.path.insert(0, str(ROOT / "src"))
        frame = trial_image(ROOT / "previews/morning.png")
        with Image.open(before) as previous:
            display, model = adapter(float(previous.info["inkypi.saturation"]))
            if model != previous.info["inkypi.panel"]:
                raise ValueError("The panel differs from the rollback checkpoint.")
        start = monotonic()
        display.display_image(frame)
        print(f"Candidate refresh completed on {model} in {monotonic() - start:.1f}s.", flush=True)
        return

    sys.path.insert(0, str(LIVE))
    if action == "restore":
        with Image.open(before) as previous:
            display, model = adapter(float(previous.info["inkypi.saturation"]))
            if model != previous.info["inkypi.panel"] or previous.size != SIZE:
                raise ValueError("The saved picture belongs to a different panel.")
            display.display_image(previous.copy())
        before.replace(state / "last-restored.png")
        return

    if before.exists():
        raise FileExistsError("Restore the outstanding checkpoint before running another trial.")
    with opened_file("settings", LIVE / "config/device.json") as f:
        config = json.load(f)
    if config.get("display_type", "inky") != "inky" or tuple(map(int, config.get("resolution") or SIZE)) != SIZE:
        raise ValueError("The installed display configuration does not match an Impression 7.3-inch panel.")
    saturation = float((config.get("image_settings") or {}).get("inky_saturation", 0.5))
    if not math.isfinite(saturation) or not 0 <= saturation <= 1:
        raise ValueError("Cannot reproduce the installed saturation setting.")
    with opened_file("preview", LIVE / "static/images/current_image.png") as f, Image.open(f) as source:
        frame = saved_frame(source.copy(), config, LIVE)
    # Detection sets only the driver's in-memory border value.
    _, model = adapter(saturation)
    save_checkpoint(state, frame, saturation, model)
    subprocess.run([sys.executable, "-IB", __file__, "draw", str(state)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "test", "draw", "restore"])
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.action == "check":
        trial_image(args.path)
        print(f"Installed Inky {version('inky')}, Pillow {version('Pillow')}.", flush=True)
    else:
        active = subprocess.check_output(["systemctl", "show", "-p", "ActiveState", "--value",
                                          "inkypi.service"], text=True).strip()
        if not os.environ.get("INVOCATION_ID") or os.geteuid() == 0 or active != "inactive":
            raise SystemExit("Use panel-trial.sh to reserve the panel for an unprivileged worker.")
        worker(args.action, args.path)
