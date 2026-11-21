import logging
from inspect import signature
import warnings
from display.abstract_display import AbstractDisplay


logger = logging.getLogger(__name__)

class InkyDisplay(AbstractDisplay):

    """
    Handles the Inky e-paper display.

    This class initializes and manages interactions with the Inky display,
    ensuring proper image rendering and configuration storage.

    The Inky display driver supports auto configuration.
    """
   
    def initialize_display(self):
        
        """
        Initializes the Inky display device.

        Sets the display border and stores the display resolution in the device configuration.

        Raises:
            ValueError: If the resolution cannot be retrieved or stored.
        """
        
        from inky.auto import auto
        self.inky_display = auto()
        self._supports_saturation = "saturation" in signature(self.inky_display.set_image).parameters
        self.inky_display.set_border(self.inky_display.BLACK)

        # store display resolution in device config
        if not self.device_config.get_config("resolution"):
            self.device_config.update_value(
                "resolution",
                [int(self.inky_display.width), int(self.inky_display.height)], 
                write=True)

    def display_image(self, image, image_settings=(), *, saturation=None):
        
        """
        Displays the provided image on the Inky display.

        The image has been processed by adjusting orientation and resizing 
        before being sent to the display.

        Args:
            image (PIL.Image): The image to be displayed.
            image_settings (list, optional): Additional settings to modify image rendering.

        Raises:
            ValueError: If no image is provided.
        """

        logger.info("Displaying image to Inky display.")
        if not image:
            raise ValueError(f"No image provided.")

        # Display the image on the Inky display
        inky_saturation = saturation
        if inky_saturation is None:
            inky_saturation = self.device_config.get_config('image_settings').get("inky_saturation", 0.5)
        logger.info(f"Inky Saturation: {inky_saturation}")
        if self._supports_saturation:
            self.inky_display.set_image(image, saturation=inky_saturation)
        else:
            self.inky_display.set_image(image)
        # v2.4.0 can report an actual BUSY deadline as a warning and return.
        # Keep the vendor's separate held-high fallback and normal waits intact.
        with warnings.catch_warnings():
            warnings.filterwarnings("error", message=r"^Busy Wait: Timed out after ",
                                    category=UserWarning, module=r"^inky(?:\.|$)")
            self.inky_display.show()
