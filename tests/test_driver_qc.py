import sys
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock
import warnings

from PIL import Image
import pytest

from display.display_manager import DisplayManager
from display.inky_display import InkyDisplay
from display.panel_io import PanelIO, PanelUnavailable
from display.waveshare_display import WaveshareDisplay
from utils.image_utils import apply_image_enhancement, change_orientation, resize_image


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
    with pytest.raises(UserWarning, match="Timed out"):
        backend.display_image(image, saturation=0.5)
    backend.inky_display.show.side_effect = lambda: vendor_warning("Busy Wait: Held high. Waiting for 40.00s")
    with pytest.warns(UserWarning, match="Held high"):
        backend.display_image(image, saturation=0.5)


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
    with pytest.raises(OSError, match="transfer error"):
        backend.display_image(frame)
    assert backend.epd_display.sleep.call_count == 2
    backend.epd_display_init.return_value = -1
    with pytest.raises(OSError, match="initialization"):
        backend.display_image(frame)
    assert backend.epd_display.Clear.call_count == 2
