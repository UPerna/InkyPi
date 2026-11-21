"""Verify both 7.3-inch Impression driver encodings of a synthetic dashboard PNG."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from inky import inky_ac073tc1a, inky_e673
import numpy as np
from PIL import Image


def capture_refresh(driver, show):
    """Run vendor packing and refresh sequencing up to the hardware commands."""
    with (patch.object(driver, "setup"),
          patch.object(driver, "_busy_wait"),
          patch.object(driver, "_send_command") as send):
        show()
    # Both 7.3-inch controllers write RAM, power on, refresh, then power off.
    commands = [call.args[0] for call in send.call_args_list]
    assert [c for c in commands if c in (0x10, 0x04, 0x12, 0x02)] == [0x10, 0x04, 0x12, 0x02]
    data = send.call_args_list[commands.index(0x10)].args[1]
    return bytes(data), commands


def check_frame(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        if image.size != (800, 480):
            raise ValueError("Expected an 800 x 480 frame")
        frame = image.convert("RGB")
    result = {}
    # The AC073TC1A translates its internal clean code (7) to white (1)
    # before the data command. Spectra 6 skips native code 4.
    for vendor, codes, blue_code in ((inky_ac073tc1a, (0, 1, 2, 3, 4, 5, 6), 3),
                                     (inky_e673, (0, 1, 2, 3, 5, 6), 5)):
        model = vendor.__name__.split(".")[-1]
        with patch.object(vendor.eeprom, "read_eeprom", return_value=None):
            driver = vendor.Inky(resolution=frame.size)
        driver.set_image(frame, saturation=0.5)
        packed, commands = capture_refresh(driver, driver.show)
        assert len(packed) == frame.width * frame.height // 2
        pairs = np.frombuffer(packed, dtype=np.uint8).reshape(frame.height, -1)
        pixels = np.empty((frame.height, frame.width), dtype=np.uint8)
        pixels[:, ::2] = pairs >> 4
        pixels[:, 1::2] = pairs & 0x0F
        expected = np.where(driver.buf == 7, 1, driver.buf) if vendor is inky_ac073tc1a else driver.buf
        np.testing.assert_array_equal(pixels, expected)
        used = set(int(i) for i in np.unique(pixels))
        assert used.issubset(codes) and {0, blue_code}.issubset(used)
        assert int(pixels[0, 0]) == 1
        # Colour each native code by its nominal pigment. The physical panel
        # appearance still depends on the panel and ambient light.
        palette = np.zeros((16, 3), dtype=np.uint8)
        palette[list(codes)] = driver.DESATURATED_PALETTE[:len(codes)]
        Image.fromarray(palette[pixels]).save(destination / f"{model}.png")
        result[model] = {"packed_bytes": len(packed), "native_codes": sorted(used),
                         "commands": [hex(command) for command in commands]}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(check_frame(args.source, args.destination), indent=2))
