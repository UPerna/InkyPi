"""
Adaptive Image Loader for InkyPi
Centralized image loading and processing with device-aware optimizations.

Automatically uses memory-efficient strategies on low-RAM devices (Pi Zero)
and high-performance strategies on capable devices (Pi 3/4).
"""

from PIL import Image, ImageOps
from utils.http_client import get_http_session, bounded_chunks
import logging
import psutil
import tempfile
import time

logger = logging.getLogger(__name__)


def _is_low_resource_device():
    """
    Detect if running on a low-resource device (e.g., Raspberry Pi Zero).
    Returns True if device has less than 1GB RAM, False otherwise.
    """
    try:
        total_memory_gb = psutil.virtual_memory().total / (1024 ** 3)
        is_low_resource = total_memory_gb < 1.0
        logger.debug(f"Device RAM: {total_memory_gb:.2f}GB - Low resource mode: {is_low_resource}")
        return is_low_resource
    except Exception as e:
        # If we can't detect, assume low resource to be safe
        logger.warning(f"Could not detect device memory: {e}. Defaulting to low-resource mode.")
        return True


class AdaptiveImageLoader:
    """
    Centralized image loading with device-adaptive optimizations.

    Features:
    - Automatic device detection (low-resource vs high-performance)
    - Memory-efficient loading using temp files + PIL draft mode on Pi Zero
    - Fast in-memory loading on powerful devices
    - Automatic resizing with quality-appropriate filters
    - RGB conversion for e-ink compatibility
    - Comprehensive error handling and logging

    Usage:
        loader = AdaptiveImageLoader()
        image = loader.from_url("https://...", (800, 480))
        image = loader.from_file("/path/to/image.jpg", (800, 480))
    """

    # Default headers to avoid 403 errors from sites that block requests without User-Agent
    DEFAULT_HEADERS = {
        'User-Agent': 'InkyPi/1.0 (https://github.com/fatihak/InkyPi/) Python-requests'
    }

    def __init__(self):
        self.is_low_resource = _is_low_resource_device()

    MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
    MAX_SOURCE_PIXELS = 40_000_000

    def from_url(self, url, dimensions, timeout_ms=40000, resize=True, headers=None):
        """Stream into a bounded seekable file, then validate before decoding."""
        timeout = timeout_ms / 1000
        deadline = time.monotonic() + timeout
        request_headers = {**self.DEFAULT_HEADERS, "Accept-Encoding": "identity", **(headers or {})}
        try:
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as stream:
                if self.is_low_resource:
                    stream.rollover()
                with get_http_session().get(url, timeout=(min(5, timeout), min(5, timeout)),
                                            stream=True, headers=request_headers) as response:
                    response.raise_for_status()
                    for chunk in bounded_chunks(response, self.MAX_DOWNLOAD_BYTES, deadline):
                        stream.write(chunk)
                stream.seek(0)
                return self._decode(stream, dimensions, resize)
        except Exception as exc:
            # Raw urllib3 streams can also raise protocol or decoding exceptions.
            logger.warning("Remote image transfer failed (%s)", type(exc).__name__)
            return None

    def from_file(self, path, dimensions, resize=True):
        try:
            with open(path, "rb") as stream:
                return self._decode(stream, dimensions, resize)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            logger.warning("Could not load local image (%s)", type(exc).__name__)
            return None

    def from_bytesio(self, data, dimensions, resize=True):
        try:
            return self._decode(data, dimensions, resize)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            logger.warning("Could not decode image (%s)", type(exc).__name__)
            return None

    def _decode(self, stream, dimensions, resize):
        with Image.open(stream) as img:
            original_size = img.size
            if img.width * img.height > self.MAX_SOURCE_PIXELS:
                raise ValueError("Source exceeds the pixel limit")
            if resize and self.is_low_resource:
                img.draft("RGB", (dimensions[0] * 2, dimensions[1] * 2))
            decoded_limit = 8_000_000 if self.is_low_resource else 32_000_000
            if img.width * img.height > decoded_limit:
                raise ValueError("Decoded image exceeds the pixel limit")
            if resize:
                if dimensions[0] * dimensions[1] > decoded_limit:
                    raise ValueError("Requested output exceeds the pixel limit")
                return self._process_and_resize(img, dimensions, original_size)
            # Pillow returns a detached image, including when there is no rotation.
            return ImageOps.exif_transpose(img)

    def _process_and_resize(self, img, dimensions, original_size):
        """
        Process and resize image with device-appropriate optimizations.

        Args:
            img: PIL Image object
            dimensions: Target dimensions (width, height)
            original_size: Original image size for logging

        Returns:
            Processed and resized PIL Image
        """
        # Apply EXIF orientation correction first (before any processing)
        # This handles images from cameras/phones that store rotation in EXIF metadata
        # Safe to call on any image - returns unchanged if no EXIF data present
        img = ImageOps.exif_transpose(img)
        if img.size != original_size:
            logger.debug(f"EXIF orientation applied: {original_size[0]}x{original_size[1]} -> {img.size[0]}x{img.size[1]}")
        
        # Convert to RGB if necessary (removes alpha channel, saves memory)
        # E-ink displays don't need alpha channel anyway
        if img.mode in ('RGBA', 'LA', 'P'):
            logger.debug(f"Converting image from {img.mode} to RGB")
            img = img.convert('RGB')

        # Choose processing strategy based on device capabilities
        if self.is_low_resource:
            img = self._resize_low_resource(img, dimensions)
        else:
            img = self._resize_high_performance(img, dimensions)

        logger.info(f"Image processing complete: {dimensions[0]}x{dimensions[1]}")
        return img

    def _resize_low_resource(self, img, dimensions):
        """Memory-efficient resize for low-resource devices."""
        logger.debug("Using memory-efficient processing (BICUBIC filter)")

        # For very large images, use two-stage resize
        if img.size[0] > dimensions[0] * 2 or img.size[1] > dimensions[1] * 2:
            logger.debug(f"Image is {img.size[0]}x{img.size[1]}, using two-stage resize")

            # Stage 1: Aggressive downsample using thumbnail (in-place, very memory efficient)
            aspect = img.size[0] / img.size[1]
            if aspect > 1:  # Landscape
                intermediate_size = (dimensions[0] * 2, max(1, int(dimensions[0] * 2 / aspect)))
            else:  # Portrait
                intermediate_size = (max(1, int(dimensions[1] * 2 * aspect)), dimensions[1] * 2)

            logger.debug(f"Stage 1: Downsampling to ~{intermediate_size[0]}x{intermediate_size[1]} using NEAREST")
            img.thumbnail(intermediate_size, Image.NEAREST)
            logger.debug(f"Stage 1 complete: {img.size[0]}x{img.size[1]}")

            # Stage 2: High-quality resize to exact dimensions
            logger.debug(f"Stage 2: Final resize to {dimensions[0]}x{dimensions[1]} using LANCZOS")
            img = ImageOps.fit(img, dimensions, method=Image.LANCZOS)
            logger.debug(f"Stage 2 complete: {dimensions[0]}x{dimensions[1]}")
        else:
            # Direct resize with BICUBIC (fast, sufficient quality for e-ink)
            logger.debug(f"Resizing directly from {img.size[0]}x{img.size[1]} to {dimensions[0]}x{dimensions[1]}")
            img = ImageOps.fit(img, dimensions, method=Image.BICUBIC)

        return img

    def _resize_high_performance(self, img, dimensions):
        """High-quality resize for powerful devices."""
        logger.debug("Using high-quality processing (LANCZOS filter)")
        logger.debug(f"Resizing from {img.size[0]}x{img.size[1]} to {dimensions[0]}x{dimensions[1]}")

        return ImageOps.fit(img, dimensions, method=Image.LANCZOS)

