from PIL import Image, ImageEnhance, ImageOps, ImageFilter
import os
import logging
import hashlib
import tempfile
import subprocess
import signal
from pathlib import Path
import time
from utils.render_budget import check_headroom, memory_limits
import shutil

logger = logging.getLogger(__name__)

def get_image(image_url):
    from utils.image_loader import AdaptiveImageLoader
    return AdaptiveImageLoader().from_url(image_url, None, timeout_ms=30000, resize=False)

def change_orientation(image, orientation, inverted=False):
    if orientation == 'horizontal':
        angle = 0
    elif orientation == 'vertical':
        angle = 90

    if inverted:
        angle = (angle + 180) % 360

    return image.rotate(angle, expand=1)

def resize_image(image, desired_size, image_settings=[]):
    img_width, img_height = image.size
    desired_width, desired_height = desired_size
    desired_width, desired_height = int(desired_width), int(desired_height)

    img_ratio = img_width / img_height
    desired_ratio = desired_width / desired_height

    keep_width = "keep-width" in image_settings

    x_offset, y_offset = 0,0
    new_width, new_height = img_width,img_height
    # Step 1: Determine crop dimensions
    desired_ratio = desired_width / desired_height
    if img_ratio > desired_ratio:
        # Image is wider than desired aspect ratio
        new_width = int(img_height * desired_ratio)
        if not keep_width:
            x_offset = (img_width - new_width) // 2
    else:
        # Image is taller than desired aspect ratio
        new_height = int(img_width / desired_ratio)
        if not keep_width:
            y_offset = (img_height - new_height) // 2

    # Step 2: Crop the image
    image = image.crop((x_offset, y_offset, x_offset + new_width, y_offset + new_height))

    # Step 3: Resize to the exact desired dimensions (if necessary)
    return image.resize((desired_width, desired_height), Image.LANCZOS)

def apply_image_enhancement(img, image_settings={}):
    # Convert image to RGB mode if necessary for enhancement operations
    # ImageEnhance requires RGB mode for operations like blend
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')

    # Each enhance() call blends over the full image, so skip factors of 1.0
    # (the no-op default) to avoid wasted full-frame passes on low-resource devices.
    enhancements = [
        (ImageEnhance.Brightness, image_settings.get("brightness", 1.0)),
        (ImageEnhance.Contrast, image_settings.get("contrast", 1.0)),
        (ImageEnhance.Color, image_settings.get("saturation", 1.0)),
        (ImageEnhance.Sharpness, image_settings.get("sharpness", 1.0)),
    ]
    for enhancer, factor in enhancements:
        if factor != 1.0:
            img = enhancer(img).enhance(factor)

    return img

def compute_image_hash(image):
    """Compute SHA-256 hash of an image."""
    if image.mode != "RGB":
        image = image.convert("RGB")
    img_bytes = image.tobytes()
    return hashlib.sha256(img_bytes).hexdigest()

def take_screenshot_html(html_str, dimensions, timeout_ms=None):
    with tempfile.TemporaryDirectory(prefix="inkypi-html-") as directory:
        page = Path(directory) / "page.html"
        page.write_text(html_str, encoding="utf-8")
        return take_screenshot(page.as_uri(), dimensions, timeout_ms)

def _find_chromium_binary():
    """Find the first available Chromium-based binary in system PATH."""
    candidates = ["chromium-headless-shell", "chromium", "google-chrome", "chrome"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            logger.debug(f"Found browser binary: {candidate} at {path}")
            return candidate
    return None


SCREENSHOT_DEADLINE = 90


def _screenshot_command(engine, browser, target, dimensions, profile_dir, output, timeout_ms):
    if engine == "firefox":
        return [browser, "--headless", "--no-remote", "--profile", profile_dir,
                "--window-size", f"{dimensions[0]},{dimensions[1]}", "--screenshot", output, target]
    command = [
        browser,
        target,
        "--headless",
        f"--user-data-dir={profile_dir}",
        f"--screenshot={output}",
        f"--window-size={dimensions[0]},{dimensions[1]}",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--use-gl=swiftshader",
        "--hide-scrollbars",
        "--in-process-gpu",
        "--js-flags=--jitless",
        "--disable-zero-copy",
        "--disable-gpu-memory-buffer-compositor-resources",
        "--disable-extensions",
        "--disable-plugins",
        "--mute-audio",
        "--renderer-process-limit=1",
        "--no-zygote",
        "--no-sandbox"
    ]
    if timeout_ms:
        command.append(f"--timeout={timeout_ms}")
    return command


def take_screenshot(target, dimensions, timeout_ms=None, *, engine="chromium"):
    img_file_path = None
    profile = None
    try:
        if engine == "firefox":
            browser = shutil.which("firefox-esr") or shutil.which("firefox")
        elif engine == "chromium":
            browser = _find_chromium_binary()
        else:
            raise ValueError("Unknown screenshot engine")
        if not browser:
            logger.error("Screenshot engine %s is unavailable", engine)
            return None

        # Create a temporary output file for the screenshot
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_file:
            img_file_path = img_file.name
        profile = tempfile.TemporaryDirectory(prefix="inkypi-browser-")

        command = _screenshot_command(engine, browser, target, dimensions, profile.name, img_file_path, timeout_ms)
        limits = memory_limits()
        check_headroom(limits)
        wall_timeout = SCREENSHOT_DEADLINE
        if engine == "firefox" and timeout_ms:
            wall_timeout = min(wall_timeout, timeout_ms / 1000)
        deadline = time.monotonic() + wall_timeout
        with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=(os.name == "posix")) as process:
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(browser, wall_timeout)
                    try:
                        process.wait(timeout=min(0.1, remaining) if limits else remaining)
                        break
                    except subprocess.TimeoutExpired:
                        check_headroom(limits)
            except (MemoryError, subprocess.TimeoutExpired):
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
                raise

        # Check if the process failed or the output file is missing
        if process.returncode != 0 or not os.path.exists(img_file_path):
            logger.error(f"Failed to take screenshot (return code: {process.returncode})")
            return None

        # Load the image using PIL
        with Image.open(img_file_path) as img:
            if engine == "firefox" and img.size != tuple(dimensions):
                raise ValueError("Unexpected Firefox viewport size")
            return img.copy()
    except (OSError, ValueError, MemoryError, subprocess.TimeoutExpired) as exc:
        logger.error("Screenshot failed (%s)", type(exc).__name__)
        return None
    finally:
        if img_file_path:
            Path(img_file_path).unlink(missing_ok=True)
        if profile is not None:
            profile.cleanup()

def pad_image_blur(img: Image, dimensions: tuple[int, int]) -> Image:
    bkg = ImageOps.fit(img, dimensions)
    bkg = bkg.filter(ImageFilter.BoxBlur(8))
    img = ImageOps.contain(img, dimensions)

    img_size = img.size
    bkg.paste(img, ((dimensions[0] - img_size[0]) // 2, (dimensions[1] - img_size[1]) // 2))
    return bkg
