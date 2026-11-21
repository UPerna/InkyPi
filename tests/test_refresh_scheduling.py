from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from unittest.mock import Mock

from flask import Flask
from jinja2 import FileSystemLoader
from PIL import Image
import pytest

from blueprints.main import main_bp
from blueprints.playlist import playlist_bp
from blueprints.plugin import plugin_bp
from config import Config
from display.display_manager import DisplayManager
from display.waveshare_display import WaveshareDisplay
from model import PluginInstance, RefreshInfo
from plugins.clock.clock import Clock
from refresh_task import ManualRefresh, RefreshTask


@pytest.fixture
def rig(tmp_path, monkeypatch):
    source = Path(Config.BASE_DIR) / "config/device_dev.json"
    data = json.loads(source.read_text())
    data.update(resolution=[8, 4], output_dir=str(tmp_path), timezone="UTC")
    config_path = tmp_path / "device.json"
    config_path.write_text(json.dumps(data))
    monkeypatch.setattr(Config, "config_file", str(config_path))
    config = Config()
    config.current_image_file = str(tmp_path / "current.png")
    config.plugin_image_dir = str(tmp_path)
    plugin = Mock(config={"image_settings": []})
    plugin.generate_image.side_effect = lambda settings, _: Image.new("RGB", (8, 4), settings.get("color", "red"))
    monkeypatch.setattr("refresh_task.get_plugin_instance", lambda _: plugin)
    manager = DisplayManager(config)
    worker = RefreshTask(config, manager)
    app = Flask(__name__)
    app.jinja_loader = FileSystemLoader([Path(Config.BASE_DIR) / "templates", Path(Config.BASE_DIR) / "plugins"])
    app.config.update(DEVICE_CONFIG=config, REFRESH_TASK=worker)
    app.register_blueprint(main_bp)
    app.register_blueprint(plugin_bp)
    app.register_blueprint(playlist_bp)
    yield worker, manager, config, app.test_client()
    worker.stop()


def finish(client, response):
    assert response.status_code == 202
    assert response.location == response.json["status_url"]
    deadline = monotonic() + 5
    while monotonic() < deadline:
        status = client.get(response.location)
        assert status.status_code == 200
        if status.json["state"] not in {"queued", "running"}:
            return status.json
        sleep(0.005)
    pytest.fail("Refresh did not finish")


def submit(client, color="red", force=False):
    url = "/update_now?force_display=1" if force else "/update_now"
    return client.post(url, data={"plugin_id": "clock", "color": color})


def test_clock_form_and_playlist_render_with_refresh_controls(rig, monkeypatch):
    _, _, _, client = rig
    monkeypatch.setattr("blueprints.plugin.get_plugin_instance", Clock)
    form = client.get("/plugin/clock")
    assert form.status_code == 200
    assert b"Force panel refresh" in form.data and b"/update_now" in form.data
    playlist = client.get("/playlist")
    assert playlist.status_code == 200
    assert b"/display_plugin_instance" in playlist.data and b"trackRefresh(" in playlist.data


def test_manual_burst_while_scheduled_driver_is_blocked(rig, capsys):
    worker, manager, config, client = rig
    playlist = config.get_playlist_manager().playlists[0]
    playlist.plugins.append(PluginInstance("clock", "morning", {}, {"interval": 3600}))
    old = datetime.now(timezone.utc) - timedelta(days=1)
    config.refresh_info = RefreshInfo("Playlist", "clock", old.isoformat(), "old-hash")
    entered, release = Event(), Event()
    colors = []

    def panel(frame, options):
        colors.append(frame.getpixel((0, 0)))
        entered.set()
        assert release.wait(10)

    manager.display.display_image = panel
    worker.start()
    try:
        assert entered.wait(3)
        assert client.get("/api/current_image").status_code == 404
        latencies, requests = [], []
        for i in range(50):
            start = monotonic()
            requests.append(submit(client, "blue" if i == 49 else "green"))
            latencies.append(monotonic() - start)
        p95 = sorted(latencies)[47]
        with capsys.disabled():
            print(f"\nBusy mock driver: 50 Flask test-client submissions, p95={p95 * 1000:.2f} ms")
        assert p95 < 1
        assert all(finish(client, req)["state"] == "superseded" for req in requests[:-1])
        assert client.get(requests[-1].location).json["state"] == "queued"
        assert colors == [(255, 0, 0)]
    finally:
        release.set()
    assert finish(client, requests[-1])["state"] == "completed"
    assert colors == [(255, 0, 0), (0, 0, 255)]
    assert playlist.current_plugin_index == 0


def test_failed_write_preserves_confirmed_frame_and_allows_retry(rig):
    worker, manager, config, client = rig
    panel = Mock()
    manager.display.display_image = panel
    worker.start()
    assert finish(client, submit(client))["state"] == "completed"
    original_png = client.get("/api/current_image").data
    assert finish(client, submit(client))["message"] == "Image already displayed."
    previous = config.get_refresh_info().to_dict()
    assert panel.call_count == 1
    panel.side_effect = OSError("private data never belongs in public status")
    result = finish(client, submit(client, "green"))
    assert result["state"] == "failed" and "private" not in str(result)
    assert client.get("/api/current_image").data == original_png
    assert config.get_refresh_info().to_dict() == previous
    assert Config().get_refresh_info().to_dict() == previous
    panel.side_effect = None
    assert finish(client, submit(client))["message"] == "Display updated."
    assert finish(client, submit(client, force=True))["message"] == "Display updated."
    assert panel.call_count == 4


def test_preparation_orientation_saturation_and_restart(rig):
    _, manager, config, _ = rig
    manager.display_type = "inky"
    panel = Mock()
    manager.display.display_image = panel
    sample = Image.new("RGB", (8, 4))
    sample.putpixel((0, 0), (255, 255, 255))
    _, _, first = manager.prepare_frame(sample, [])
    assert not Path(config.current_image_file).exists()
    panel.assert_not_called()
    assert manager.display_image(sample, []) == (first, True)
    assert manager.display_image(sample, []) == (first, False)
    config.update_value("inverted_image", True)
    second, wrote = manager.display_image(sample, [])
    assert wrote and second != first
    with Image.open(config.current_image_file) as confirmed:
        assert confirmed.getpixel((7, 3)) == (255, 255, 255)
    config.update_value("image_settings", {"inky_saturation": 0.8})
    third, wrote = manager.display_image(sample, [])
    assert wrote and third != second
    assert panel.call_args.kwargs == {"saturation": 0.8}
    config.update_value("orientation", "vertical")
    assert manager.display_image(sample, [])[1]
    restarted = DisplayManager(config)
    assert restarted.display_image(sample, [])[1]


def test_stopped_worker_and_saved_playlist_endpoint(rig):
    worker, _, config, client = rig
    assert submit(client).status_code == 503
    assert client.get("/api/refresh/missing").status_code == 404
    playlist = config.get_playlist_manager().playlists[0]
    playlist.plugins.append(PluginInstance("clock", "one", {}, {"interval": 600}))
    worker.start()
    req = client.post("/display_plugin_instance", json={"playlist_name": playlist.name,
                      "plugin_id": "clock", "plugin_instance": "one"})
    assert finish(client, req)["state"] == "completed"
    assert config.get_refresh_info().plugin_instance == "one"


def test_waveshare_driver_exception_preserves_last_frame(rig):
    _, manager, _, client = rig
    # Use the real adapter with a simulated SPI device.
    manager.display_type = "epd7in3"
    adapter = object.__new__(WaveshareDisplay)
    adapter.epd_display_init = Mock()
    adapter.epd_display = Mock()
    adapter.bi_color_display = False
    manager.display = adapter
    manager.display_image(Image.new("RGB", (8, 4), "white"))
    old = client.get("/api/current_image").data
    adapter.epd_display.display.side_effect = OSError("transfer failed")
    with pytest.raises(OSError):
        manager.display_image(Image.new("RGB", (8, 4), "black"))
    assert client.get("/api/current_image").data == old
    adapter.epd_display.display.side_effect = None
    assert manager.display_image(Image.new("RGB", (8, 4), "white"))[1]
    assert adapter.epd_display.display.call_count == 3


def test_failed_scheduled_write_retries_same_playlist_item(rig):
    worker, manager, config, _ = rig
    playlist = config.get_playlist_manager().playlists[0]
    first = PluginInstance("clock", "first", {}, {"interval": 3600})
    second = PluginInstance("clock", "second", {}, {"interval": 3600})
    playlist.plugins.extend([first, second])
    now = datetime.now(timezone.utc)
    config.refresh_info = RefreshInfo("Playlist", "clock", (now - timedelta(days=1)).isoformat(), "old")
    attempts = []
    failed, success = Event(), Event()
    def panel(*_):
        attempts.append(monotonic())
        if len(attempts) == 1:
            failed.set()
            raise OSError("panel unavailable")
        success.set()
    manager.display.display_image = panel
    worker.start()
    assert failed.wait(3)
    worker.stop()
    assert playlist.current_plugin_index is None
    assert config.get_refresh_info().image_hash == "old"
    worker.start()
    assert success.wait(3)
    worker.stop()
    assert len(attempts) == 2
    assert playlist.current_plugin_index == 0
    assert config.get_refresh_info().plugin_instance == "first"
    assert not Path(config.plugin_image_dir, second.get_image_path()).exists()


def test_shutdown_resolves_pending_request_while_active_driver_finishes(rig):
    worker, manager, _, _ = rig
    entered, release = Event(), Event()
    def panel(*_):
        entered.set()
        assert release.wait(5)
    manager.display.display_image = panel
    worker.start()
    active = worker.submit(ManualRefresh("clock", {}))
    assert entered.wait(3)
    pending = worker.submit(ManualRefresh("clock", {}))
    stopping = Thread(target=worker.stop)
    stopping.start()
    try:
        assert pending.future.result(timeout=1)["state"] == "failed"
        assert not active.future.done()
    finally:
        release.set()
        stopping.join(5)
    assert active.future.result(timeout=1)["state"] == "completed"
    assert not stopping.is_alive()


def test_daily_plugin_waits_for_its_local_scheduled_time():
    early = datetime(2026, 11, 21, 7, 10, tzinfo=timezone.utc)
    plugin = PluginInstance("clock", "morning", {}, {"scheduled": "08:00"}, early.isoformat())
    assert not plugin.should_refresh(early + timedelta(minutes=10))
    assert plugin.should_refresh(early.replace(hour=8, minute=0))
    plugin.latest_refresh_time = early.replace(hour=8, minute=1).isoformat()
    assert not plugin.should_refresh(early.replace(hour=10))


def test_overlapping_uploads_keep_their_own_files(rig, monkeypatch):
    worker, _, _, client = rig
    started, release = Event(), Event()
    reads = []
    def render(settings, _):
        path = Path(settings["imageFiles[]"][0])
        started.set()
        assert release.wait(5)
        reads.append((path, path.read_bytes()))
        return Image.new("RGB", (8, 4), "red")
    plugin = Mock(config={})
    plugin.generate_image.side_effect = render
    monkeypatch.setattr("refresh_task.get_plugin_instance", lambda _: plugin)
    def upload(data):
        return client.post("/update_now", data={"plugin_id": "clock",
                           "imageFiles[]": (BytesIO(data), "same.png")})
    worker.start()
    try:
        first = upload(b"first")
        assert started.wait(2)
        replaced = upload(b"replaced")
        last = upload(b"last")
        assert finish(client, replaced)["state"] == "superseded"
    finally:
        release.set()
    assert finish(client, first)["state"] == finish(client, last)["state"] == "completed"
    assert [data for _, data in reads] == [b"first", b"last"]
    worker.stop()
    assert all(not path.exists() for path, _ in reads)
