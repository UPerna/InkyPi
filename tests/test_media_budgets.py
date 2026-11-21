from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import os
from pathlib import Path
import sys
from threading import Thread
import time
from unittest.mock import Mock

import psutil
from PIL import Image
import pytest

from plugins.wpotd.wpotd import Wpotd
from utils import image_utils
from utils.image_loader import AdaptiveImageLoader
from utils.render_budget import check_headroom, memory_limits


@pytest.fixture
def local_server():
    image = BytesIO()
    Image.new("RGB", (16, 8), "red").save(image, "PNG")
    png = image.getvalue()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            if self.path == "/declared":
                self.send_header("Content-Length", "10000")
            elif self.path == "/short":
                self.send_header("Content-Length", str(len(png) + 10))
            self.end_headers()
            try:
                if self.path == "/slow":
                    for byte in png:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                else:
                    self.wfile.write(png + (b"x" * 500 if self.path == "/unknown" else b""))
            except (ConnectionResetError, BrokenPipeError):
                pass
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("low_memory", [False, True])
def test_real_streams_are_bounded_and_next_fetch_recovers(local_server, low_memory, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    loader = AdaptiveImageLoader()
    loader.is_low_resource = low_memory
    loader.MAX_DOWNLOAD_BYTES = 200
    for suffix in ("declared", "unknown", "short", "slow"):
        start = time.monotonic()
        assert loader.from_url(f"{local_server}/{suffix}", (8, 4), timeout_ms=150) is None
        assert time.monotonic() - start < 2
    assert loader.from_url(local_server + "/ok", (8, 4)).getpixel((0, 0)) == (255, 0, 0)
    assert list(tmp_path.iterdir()) == []


def test_pixel_limit_precedes_raster_decode(tmp_path, monkeypatch):
    data = BytesIO()
    Image.new("RGB", (11, 10)).save(data, "PNG")
    path = tmp_path / "header.png"
    path.write_bytes(data.getvalue())
    loader = AdaptiveImageLoader()
    loader.MAX_SOURCE_PIXELS = 100
    def unexpected(*args, **kwargs):
        pytest.fail("Pillow decoded a rejected image")
    monkeypatch.setattr("PIL.PngImagePlugin.PngImageFile.load", unexpected)
    assert loader.from_file(path, (2, 2)) is None
    assert loader.from_bytesio(data, (2, 2), resize=False) is None


@pytest.mark.parametrize("resize", [False, True])
@pytest.mark.parametrize("name", ["clip.WEBM?download=1", "audio.ogg", "drawing.SVG?raw=1", "book.pdf"])
def test_wikipedia_rejects_non_images_before_fetch(name, resize):
    plugin = Wpotd.__new__(Wpotd)
    plugin.image_loader = Mock()
    with pytest.raises(RuntimeError, match="media type"):
        plugin._download_image("https://example.invalid/" + name, (80, 48), resize)
    plugin.image_loader.from_url.assert_not_called()


def test_wikipedia_bounded_dated_fallback_and_recovery(tmp_path):
    cfg = Mock(plugin_image_dir=str(tmp_path))
    cfg.get_resolution.return_value = (160, 96)
    cfg.get_config.return_value = "horizontal"
    plugin = Wpotd.__new__(Wpotd)
    plugin._fetch_potd = Mock(return_value={"image_src": "local"})
    plugin._download_image = Mock(return_value=Image.new("RGB", (160, 96), "blue"))
    plugin._determine_date = Mock(return_value=date(2026, 11, 20))
    plugin.generate_image({}, cfg)
    cached = tmp_path / ".wpotd-last.png"
    with Image.open(cached) as stored:
        assert stored.info["source_date"] == "2026-11-20"
    original = cached.read_bytes()
    plugin._fetch_potd.side_effect = RuntimeError("offline")
    stale = plugin.generate_image({}, cfg)
    assert stale.getpixel((1, 1)) == (255, 255, 255)
    assert stale.getpixel((1, 80)) == (0, 0, 255)
    assert stale.crop((4, 3, 135, 18)).convert("L").getextrema()[0] < 128
    assert cached.read_bytes() == original
    with pytest.raises(RuntimeError):
        plugin.generate_image({"customDate": "2020-01-01"}, cfg)
    plugin._fetch_potd.side_effect = None
    plugin._download_image.return_value = Image.new("RGB", (160, 96), "red")
    assert plugin.generate_image({}, cfg).getpixel((1, 1)) == (255, 0, 0)
    assert cached.read_bytes() != original
    plugin.CACHE_LIMIT = 10
    previous = cached.read_bytes()
    plugin._save_cached(cached, "x", date.today(), stale, (160, 96))
    assert cached.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [cached]


@pytest.mark.skipif(os.name != "posix", reason="Pi uses POSIX process groups")
def test_hung_browser_and_descendant_are_stopped_and_files_released(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    marker = tmp_path / "child.pid"
    browser = tmp_path / "browser"
    browser.write_text(f"#!{sys.executable}\nimport subprocess,time\n"
                       f"p=subprocess.Popen(['sleep','30'])\nopen({str(marker)!r},'w').write(str(p.pid))\n"
                       "time.sleep(30)\n")
    browser.chmod(0o700)
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    monkeypatch.setattr(image_utils, "_find_chromium_binary", lambda: str(browser))
    monkeypatch.setattr(image_utils, "SCREENSHOT_DEADLINE", 0.5)
    start = time.monotonic()
    assert image_utils.take_screenshot_html("<p>Fixture</p>", (80, 48)) is None
    assert time.monotonic() - start < 3
    child = int(marker.read_text())
    until = time.monotonic() + 1
    while time.monotonic() < until and psutil.pid_exists(child):
        if psutil.Process(child).status() == psutil.STATUS_ZOMBIE:
            break
        time.sleep(0.01)
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE
    assert list(scratch.iterdir()) == []


def test_inherited_cgroup_limit_and_reclaimable_pages(tmp_path):
    root = tmp_path / "groups"
    leaf = root / "system.slice" / "inkypi.service"
    leaf.mkdir(parents=True)
    membership = tmp_path / "membership"
    membership.write_text("0::/system.slice/inkypi.service\n")
    (leaf / "memory.max").write_text("max")
    limit = 200 * 2**20
    (root / "memory.max").write_text(str(limit))
    (root / "memory.current").write_text(str(190 * 2**20))
    (root / "memory.stat").write_text(f"inactive_file {30 * 2**20}\n")
    limits = memory_limits(root, membership)
    assert limits == [(root, limit)]
    check_headroom(limits)
    (root / "memory.stat").write_text("inactive_file 0\n")
    with pytest.raises(MemoryError):
        check_headroom(limits)


@pytest.mark.skipif(os.name != "posix", reason="Linux render admission and process teardown")
def test_render_stops_under_memory_pressure_and_next_render_succeeds(tmp_path, monkeypatch):
    browser = tmp_path / "browser"
    current = tmp_path / "memory.current"
    marker = tmp_path / "launched"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (tmp_path / "memory.stat").write_text("inactive_file 0")
    limit = 200 * 2**20
    current.write_text(str(limit))
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    monkeypatch.setattr(image_utils, "_find_chromium_binary", lambda: str(browser))
    monkeypatch.setattr(image_utils, "memory_limits", lambda: [(tmp_path, limit)])
    browser.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport time\n"
                       f"Path({str(marker)!r}).touch()\nPath({str(current)!r}).write_text('{limit}')\n"
                       "time.sleep(30)\n")
    browser.chmod(0o700)
    assert image_utils.take_screenshot_html("test", (8, 4)) is None
    assert not marker.exists()
    current.write_text("0")
    start = time.monotonic()
    assert image_utils.take_screenshot_html("test", (8, 4)) is None
    assert marker.exists() and time.monotonic() - start < 3
    current.write_text("0")
    browser.write_text(f"#!{sys.executable}\nimport sys\nfrom PIL import Image\n"
                       "dest=next(s.split('=',1)[1] for s in sys.argv if s.startswith('--screenshot='))\n"
                       "Image.new('RGB',(8,4),'red').save(dest)\n")
    assert image_utils.take_screenshot_html("test", (8, 4)).getpixel((0, 0)) == (255, 0, 0)
    assert list(scratch.iterdir()) == []
