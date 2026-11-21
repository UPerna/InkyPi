"""Verify both 7.3-inch Impression driver encodings of a synthetic dashboard PNG."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from inky import inky_ac073tc1a, inky_e673
import numpy as np
from PIL import Image


def check_frame(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        if image.size != (800, 480):
            raise ValueError("Expected an 800 x 480 frame")
        frame = image.convert("RGB")
    result = {}
    # Spectra 6 skips native code 4. AC073TC1A also accepts 7 (clean/white).
    for vendor, codes, blue_code in ((inky_ac073tc1a, (0, 1, 2, 3, 4, 5, 6, 7), 3),
                                     (inky_e673, (0, 1, 2, 3, 5, 6), 5)):
        model = vendor.__name__.split(".")[-1]
        with patch.object(vendor.eeprom, "read_eeprom", return_value=None):
            driver = vendor.Inky(resolution=frame.size)
        driver.set_image(frame, saturation=0.5)
        # Run the vendor's actual byte packing. Intercept its hardware transfer.
        with patch.object(driver, "_update") as transfer:
            driver.show()
        transfer.assert_called_once()
        packed = bytes(transfer.call_args.args[0])
        assert len(packed) == frame.width * frame.height // 2
        pairs = np.frombuffer(packed, dtype=np.uint8).reshape(frame.height, -1)
        pixels = np.empty((frame.height, frame.width), dtype=np.uint8)
        pixels[:, ::2] = pairs >> 4
        pixels[:, 1::2] = pairs & 0x0F
        np.testing.assert_array_equal(pixels, driver.buf)
        used = set(int(i) for i in np.unique(pixels))
        assert used.issubset(codes) and {0, blue_code}.issubset(used)
        assert int(pixels[0, 0]) in ({1, 7} if vendor is inky_ac073tc1a else {1})
        # Colour each native code by its nominal pigment. The physical panel
        # appearance still depends on the panel and ambient light.
        palette = np.zeros((16, 3), dtype=np.uint8)
        palette[list(codes)] = driver.DESATURATED_PALETTE[:len(codes)]
        Image.fromarray(palette[pixels]).save(destination / f"{model}.png")
        result[model] = {"packed_bytes": len(packed), "native_codes": sorted(used)}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(check_frame(args.source, args.destination), indent=2))
