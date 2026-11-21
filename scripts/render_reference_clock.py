"""Render the stock digital Clock plugin through the mock display at a fixed time."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from config import Config
from display.display_manager import DisplayManager
from plugins.clock.clock import Clock


def render_reference(destination):
    # Use the stock development settings and isolate every generated file.
    reference_time = datetime(2026, 11, 21, 6, 35, tzinfo=timezone.utc)
    with TemporaryDirectory() as directory:
        with patch.object(Config, "config_file", str(SRC / "config" / "device_dev.json")):
            device = Config()
        device.current_image_file = str(Path(directory) / "current.png")
        device.update_value("output_dir", directory)
        device.update_value("timezone", "Europe/Rome")
        manager = DisplayManager(device)
        plugin = Clock(device.get_plugin("clock"))
        with patch("plugins.clock.clock.datetime") as clock:
            clock.now.side_effect = reference_time.astimezone
            frame = plugin.generate_image({"selectedClockFace": "Digital Clock"}, device)
        manager.display_image(frame)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(directory) / "latest.png", destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="mock_display_output/reference-clock.png")
    args = parser.parse_args()
    render_reference(args.output)
    print(args.output)
