from PIL import Image

from scripts.render_reference_clock import render_reference


def test_reference_clock_is_repeatable(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    render_reference(first)
    render_reference(second)
    assert first.read_bytes() == second.read_bytes()
    with Image.open(first) as frame:
        assert frame.size == (800, 480)
        low, high = frame.convert("L").getextrema()
        assert (low, high) == (0, 255)
