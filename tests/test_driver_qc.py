import sys
import importlib
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock
import warnings
from concurrent.futures import ThreadPoolExecutor

from PIL import Image
import pytest

from display.display_manager import DisplayManager
from display.inky_display import InkyDisplay
from display.panel_io import PanelIO, PanelUnavailable
from display.waveshare_display import WaveshareDisplay
from utils.image_utils import apply_image_enhancement, change_orientation, resize_image
from plugins import plugin_registry


@pytest.mark.parametrize(("module_name", "red_code"), [("inky_e673", 3), ("inky_ac073tc1a", 4)])
def test_pinned_impression_driver_produces_native_colour_codes(monkeypatch, module_name, red_code):
    # The PC exercises the vendor's complete pixel conversion. Only hardware
    # discovery, EEPROM access and physical transfer are replaced.
    pytest.importorskip("inky")
    vendor = importlib.import_module("inky." + module_name)
    monkeypatch.setattr(vendor.eeprom, "read_eeprom", lambda **_: None)
    chip = vendor.Inky(resolution=(800, 480))
    monkeypatch.setattr(importlib.import_module("inky.auto"), "auto", lambda: chip)
    show = Mock()
    monkeypatch.setattr(chip, "show", show)
    adapter = InkyDisplay(Mock())
    frame = Image.new("RGB", (800, 480), "white")
    frame.paste("black", (0, 0, 250, 480))
    frame.paste("red", (550, 0, 800, 480))
    adapter.display_image(frame, saturation=0.0)
    buffer = chip.buf.reshape(480, 800)
    assert (buffer[:, :250] == 0).all() and (buffer[:, 250:550] == 1).all()
    assert (buffer[:, 550:] == red_code).all()
    show.assert_called_once()


@pytest.mark.parametrize(("model", "red_blue"), [("inky_ac073tc1a", 0x43), ("inky_e673", 0x35)])
@pytest.mark.parametrize("mode", ["L", "RGB"])
def test_impression_command_payload_matches_known_pixels(monkeypatch, model, red_blue, mode):
    pytest.importorskip("inky")
    from scripts.check_impression_frame import capture_refresh
    vendor = importlib.import_module("inky." + model)
    monkeypatch.setattr(vendor.eeprom, "read_eeprom", lambda **_: None)
    chip = vendor.Inky(resolution=(800, 480))
    monkeypatch.setattr(importlib.import_module("inky.auto"), "auto", lambda: chip)
    adapter = InkyDisplay(Mock())
    frame = Image.new(mode, (800, 480), "white")
    for xy in [(0, 0), (1, 1), (799, 479)]:
        frame.putpixel(xy, 0 if mode == "L" else (0, 0, 0))
    # Native white in both nibbles, with asymmetric marks at known wire offsets.
    expected = bytearray([0x11]) * 192000
    expected[0], expected[400], expected[-1] = 0x01, 0x10, 0x10
    if mode == "RGB":
        frame.putpixel((2, 2), (255, 0, 0))
        frame.putpixel((3, 2), (0, 0, 255))
        expected[801] = red_blue
    payload, _ = capture_refresh(chip, lambda: adapter.display_image(frame, saturation=0.0))
    assert payload == expected


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("inverted", [False, True])
@pytest.mark.parametrize("mode", ["RGB", "L", "RGBA", "P"])
@pytest.mark.parametrize("source_size", [(24, 12), (33, 9)])
def test_frame_matches_reference_transform(mode, orientation, inverted, source_size):
    raw = bytes((i * 29) % 256 for i in range(source_size[0] * source_size[1] * 3))
    image = Image.frombytes("RGB", source_size, raw).convert(mode)
    before = image.tobytes()
    cfg = {"resolution": [24, 12], "orientation": orientation, "inverted_image": inverted,
           "image_settings": {"contrast": 1.15}}
    manager = DisplayManager.__new__(DisplayManager)
    manager.device_config = Mock(get_config=Mock(return_value=cfg))
    manager.display_type = "inky"
    reference = resize_image(change_orientation(image, orientation), cfg["resolution"], ["keep-width"])
    if inverted:
        reference = reference.rotate(180)
    reference = apply_image_enhancement(reference, cfg["image_settings"])
    actual, _, _ = manager.prepare_frame(image, ["keep-width"])
    assert (actual.mode, actual.size, actual.tobytes()) == (reference.mode, reference.size, reference.tobytes())
    assert image.tobytes() == before


@pytest.mark.parametrize("saturation_capable", [True, False])
def test_pimoroni_detects_set_image_signature_once(monkeypatch, saturation_capable):
    received = []
    if saturation_capable:
        def set_image(image, saturation=0.5):
            received.append(saturation)
    else:
        def set_image(image):
            received.append(None)
    chip = SimpleNamespace(BLACK=0, width=800, height=480, set_border=Mock(),
                           set_image=set_image, show=Mock())
    monkeypatch.setitem(sys.modules, "inky.auto", SimpleNamespace(auto=lambda: chip))
    cfg = Mock()
    cfg.get_config.return_value = None
    backend = InkyDisplay(cfg)
    backend.display_image(Image.new("RGB", (8, 4)), saturation=0.8)
    cfg.update_value.assert_called_once_with("resolution", [800, 480], write=True)
    assert received == ([0.8] if saturation_capable else [None])
    chip.show.assert_called_once()


def test_pimoroni_busy_timeout_and_held_high_have_distinct_outcomes():
    backend = object.__new__(InkyDisplay)
    backend._supports_saturation = True
    backend.inky_display = Mock()
    image = Image.new("RGB", (4, 4))
    def vendor_warning(message):
        warnings.warn_explicit(message, UserWarning, "inky_e673.py", 1, module="inky.inky_e673")
    backend.inky_display.show.side_effect = lambda: vendor_warning("Busy Wait: Timed out after 40.00s")
    with pytest.raises(PanelUnavailable, match="Force panel refresh") as failure:
        backend.display_image(image, saturation=0.5)
    assert isinstance(failure.value.__cause__, UserWarning)
    backend.inky_display.show.side_effect = lambda: vendor_warning("Busy Wait: Held high. Waiting for 40.00s")
    with pytest.warns(UserWarning, match="Held high"):
        backend.display_image(image, saturation=0.5)


@pytest.mark.parametrize("stored", [[600, 448], [480, 800], [800, 480]])
def test_inky_uses_detected_panel_dimensions(monkeypatch, stored):
    chip = SimpleNamespace(width=800, height=480, BLACK=0, set_border=Mock(),
                           set_image=lambda frame: None)
    monkeypatch.setitem(sys.modules, "inky.auto", SimpleNamespace(auto=lambda: chip))
    cfg = Mock()
    cfg.get_config.return_value = stored
    InkyDisplay(cfg)
    if stored == [800, 480]:
        cfg.update_value.assert_not_called()
    else:
        cfg.update_value.assert_called_once_with("resolution", [800, 480], write=True)


def test_failed_panel_transfer_requires_successful_force_to_resume():
    chip = Mock()
    backend = object.__new__(InkyDisplay)
    backend.inky_display = chip
    backend._supports_saturation = True
    io = PanelIO()
    frame = Image.new("RGB", (4, 4))
    def write(force=False):
        io.write(backend, frame, (), {"saturation": 0.5}, force=force)

    # A rejected image has not reached the hardware; a corrected image can run.
    chip.set_image.side_effect = ValueError("bad image")
    with pytest.raises(ValueError):
        write()
    chip.show.assert_not_called()
    chip.set_image.side_effect = None
    chip.show.side_effect = [OSError("SPI write interrupted"), None, None]
    with pytest.raises(PanelUnavailable, match="Force panel refresh"):
        write()
    with pytest.raises(PanelUnavailable, match="uncertain"):
        write()
    # Even a pre-transfer error on a forced retry cannot confirm the old frame.
    chip.set_image.side_effect = ValueError("bad recovery image")
    with pytest.raises(ValueError):
        write(force=True)
    chip.set_image.side_effect = None
    with pytest.raises(PanelUnavailable, match="uncertain"):
        write()
    write(force=True)
    write()
    assert chip.show.call_count == 3


def test_waveshare_prepares_buffers_before_clearing():
    panel = Mock()
    adapter = object.__new__(WaveshareDisplay)
    adapter.epd_display = panel
    adapter.epd_display_init = panel.init
    adapter.bi_color_display = True
    panel.getbuffer.side_effect = [b"black", ValueError("red buffer failed")]
    with pytest.raises(ValueError, match="buffer"):
        adapter.display_image(Image.new("RGB", (8, 4)))
    panel.init.assert_not_called()
    panel.Clear.assert_not_called()
    panel.display.assert_not_called()


def test_late_transfer_never_publishes_or_overlaps_its_retry(tmp_path):
    cfg = {"resolution": [16, 8], "display_type": "mock", "output_dir": str(tmp_path)}
    manager = DisplayManager(SimpleNamespace(get_config=lambda key=None, default=None: cfg if key is None else cfg.get(key, default),
                             get_resolution=lambda: (16, 8), current_image_file=str(tmp_path / "current.png")))
    manager.panel_io = PanelIO(0.1)
    entered, release = Event(), Event()
    colors = []
    def driver(frame, settings):
        colors.append(frame.getpixel((0, 0)))
        if len(colors) == 2:
            entered.set()
            assert release.wait(5)
    manager.display.display_image = driver
    white = Image.new("RGB", (16, 8), "white")
    black = Image.new("RGB", (16, 8), "black")
    manager.display_image(white)
    last = (tmp_path / "current.png").read_bytes()
    try:
        with pytest.raises(PanelUnavailable, match="timed out"):
            manager.display_image(black)
        assert entered.is_set()
        with pytest.raises(PanelUnavailable, match="still running"):
            manager.display_image(white, force=True)
    finally:
        release.set()
    until = monotonic() + 3
    while manager.panel_io._active is not None and monotonic() < until:
        sleep(0.005)
    with pytest.raises(PanelUnavailable, match="uncertain"):
        manager.display_image(white)
    assert (tmp_path / "current.png").read_bytes() == last
    assert not list(tmp_path.glob(".current-*"))
    assert manager.display_image(white, force=True)[1]
    assert colors == [(255, 255, 255), (0, 0, 0), (255, 255, 255)]


def test_waveshare_converts_two_planes_and_sleeps_on_error():
    backend = object.__new__(WaveshareDisplay)
    backend.epd_display = Mock()
    backend.epd_display_init = Mock(return_value=0)
    backend.epd_display.getbuffer.side_effect = lambda image: image.tobytes()
    backend.bi_color_display = True
    frame = Image.new("RGB", (3, 1))
    frame.putdata([(0, 0, 0), (255, 255, 255), (255, 0, 0)])
    backend.display_image(frame)
    backend.epd_display.display.assert_called_once_with(b"\x60", b"\xc0")
    backend.epd_display.display.side_effect = OSError("transfer error")
    backend.epd_display.sleep.side_effect = OSError("sleep error")
    with pytest.raises(PanelUnavailable) as failure:
        backend.display_image(frame)
    assert str(failure.value.__cause__) == "transfer error"
    assert backend.epd_display.sleep.call_count == 2
    backend.epd_display_init.return_value = -1
    with pytest.raises(OSError, match="initialization"):
        backend.display_image(frame)
    assert backend.epd_display.Clear.call_count == 2


def test_lazy_plugin_import_is_singleton_under_concurrent_first_use(monkeypatch):
    config = {"id": "clock", "class": "Clock"}
    constructor = Mock()
    importer = Mock(return_value=SimpleNamespace(Clock=constructor))
    monkeypatch.setattr(plugin_registry, "_registered", {})
    monkeypatch.setattr(plugin_registry, "PLUGIN_CLASSES", {})
    monkeypatch.setattr(plugin_registry.importlib, "import_module", importer)
    plugin_registry.load_plugins([config, {"id": "wpotd", "class": "Wpotd"}])
    importer.assert_not_called()
    with ThreadPoolExecutor(max_workers=8) as pool:
        instances = list(pool.map(plugin_registry.get_plugin_instance, [config] * 24))
    assert all(instance is instances[0] for instance in instances)
    importer.assert_called_once_with("plugins.clock.clock")
    constructor.assert_called_once_with(config)
    plugin_registry.load_plugins(dict(config, disabled=True))
    with pytest.raises(ValueError, match="not registered"):
        plugin_registry.get_plugin_instance(config)
