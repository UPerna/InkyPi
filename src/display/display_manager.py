import fnmatch
import hashlib
import os
from pathlib import Path
import tempfile
import json
import logging

from utils.image_utils import resize_image, change_orientation, apply_image_enhancement, compute_image_hash
from display.mock_display import MockDisplay
from display.panel_io import PanelIO

logger = logging.getLogger(__name__)

class DisplayManager:

    """Manages the display and rendering of images."""

    def __init__(self, device_config):

        """
        Initializes the display manager and selects the correct display type 
        based on the configuration.

        Args:
            device_config (object): Configuration object containing display settings.

        Raises:
            ValueError: If an unsupported display type is specified.
        """
        
        self.device_config = device_config
        # Reconcile once after every restart, including an interrupted panel write.
        self._confirmed_hash = None
     
        display_type = device_config.get_config("display_type", default="inky")

        self.display_type = display_type
        self.panel_io = None if display_type == "mock" else PanelIO()
        if display_type == "mock":
            self.display = MockDisplay(device_config)
        elif display_type == "inky":
            from display.inky_display import InkyDisplay
            self.display = InkyDisplay(device_config)
        elif fnmatch.fnmatch(display_type, "epd*in*"):  
            # derived from waveshare epd - we assume here that will be consistent
            # otherwise we will have to enshring the manufacturer in the 
            # display_type and then have a display_model parameter.  Will leave
            # that for future use if the need arises.
            #
            # see https://github.com/waveshareteam/e-Paper
            from display.waveshare_display import WaveshareDisplay
            self.display = WaveshareDisplay(device_config)
        else:
            raise ValueError(f"Unsupported display type: {display_type}")

    def prepare_frame(self, image, image_settings):
        """Prepare the panel pixels and options without updating the device or its last frame."""
        config = self.device_config.get_config().copy()
        orientation = config.get("orientation") or "horizontal"
        inverted = bool(config.get("inverted_image"))
        enhancements = dict(config.get("image_settings", {}))
        frame = image if orientation == "horizontal" else change_orientation(image, orientation)
        if frame.size != tuple(map(int, config["resolution"])):
            frame = resize_image(frame, config["resolution"], image_settings)
        if inverted:
            frame = frame.rotate(180)
        frame = apply_image_enhancement(frame, enhancements)
        driver_options = {}
        if self.display_type == "inky":
            driver_options["saturation"] = enhancements.get("inky_saturation", 0.5)

        # Include geometry and driver parameters, which raw RGB bytes alone omit.
        signature = [self.display_type, frame.size, orientation, inverted, image_settings,
                     enhancements, driver_options, compute_image_hash(frame)]
        digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        return frame, driver_options, digest

    def display_image(self, image, image_settings=(), *, force=False):
        frame, options, digest = self.prepare_frame(image, image_settings)
        path = Path(self.device_config.current_image_file)
        if not force and digest == self._confirmed_hash and path.is_file():
            return digest, False

        # Stage first so an encoding or storage error cannot start a panel write.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".current-", suffix=".png", delete=False) as tmp:
            staged = Path(tmp.name)
        try:
            frame.save(staged, format="PNG")
            self._confirmed_hash = None
            if self.panel_io is None:
                self.display.display_image(frame, image_settings, **options)
            else:
                # A late vendor call can outlive the plugin request. Own the
                # pixels it reads if preparation reused the plugin's image.
                transfer_frame = frame.copy() if frame is image else frame
                self.panel_io.write(self.display, transfer_frame, image_settings, options, force)
            os.replace(staged, path)
            self._confirmed_hash = digest
        finally:
            staged.unlink(missing_ok=True)
        return digest, True
