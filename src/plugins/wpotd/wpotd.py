"""
Wpotd Plugin for InkyPi
This plugin fetches the Wikipedia Picture of the Day (Wpotd) from Wikipedia's API
and displays it on the InkyPi device.

It supports optional manual date selection or random dates and can resize the image to fit the device's dimensions.

Wikipedia API Documentation: https://www.mediawiki.org/wiki/API:Main_page
Picture of the Day example: https://www.mediawiki.org/wiki/API:Picture_of_the_day_viewer
Github Repository: https://github.com/wikimedia/mediawiki-api-demos/tree/master/apps/picture-of-the-day-viewer
Wikimedia requires a User Agent header for API requests, which is set in the SESSION headers:
https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy

Flow:

1. Fetch the date to use for the Picture of the Day (POTD) based on settings. (_determine_date)
2. Make an API request to fetch the POTD data for that date. (_fetch_potd)
3. Extract the image filename from the response. (_fetch_potd)
4. Make another API request to get the image URL. (_fetch_image_src)
5. Download the image from the URL. (_download_image)
6. Optionally resize the image to fit the device dimensions. (_shrink_to_fit))
"""

from plugins.base_plugin.base_plugin import BasePlugin
from PIL import Image, ImageDraw, PngImagePlugin
from pathlib import Path
from urllib.parse import urlsplit
from tempfile import NamedTemporaryFile
import os
import json
import time
from utils.http_client import get_http_session, bounded_chunks
import logging
from random import randint
from datetime import datetime, timedelta, date
from typing import Dict, Any

logger = logging.getLogger(__name__)

class Wpotd(BasePlugin):
    HEADERS = {'User-Agent': 'InkyPi/1.0 (https://github.com/fatihak/InkyPi/)'}
    API_URL = "https://en.wikipedia.org/w/api.php"

    NON_IMAGE = (".svg", ".webm", ".ogv", ".ogg", ".oga", ".ogx", ".opus", ".mp4",
                 ".mpg", ".mpeg", ".mp3", ".wav", ".flac", ".mid", ".pdf", ".djvu", ".stl")
    CACHE_LIMIT = 2 * 1024 * 1024

    def generate_settings_template(self) -> Dict[str, Any]:
        template_params = super().generate_settings_template()
        template_params['style_settings'] = False
        return template_params

    def generate_image(self, settings: Dict[str, Any], device_config: Dict[str, Any]) -> Image.Image:
        logger.info("=== Wikipedia POTD Plugin: Starting image generation ===")

        datetofetch = self._determine_date(settings)
        logger.info(f"Fetching Wikipedia Picture of the Day for: {datetofetch}")
        logger.debug(f"Settings: shrink_to_fit={settings.get('shrinkToFitWpotd', 'false')}, randomize={settings.get('randomizeWpotd', 'false')}")

        width, height = map(int, device_config.get_resolution())
        if device_config.get_config("orientation") == "vertical":
            width, height = height, width
        dimensions = (width, height)
        resize = settings.get("shrinkToFitWpotd") == "true"
        selection = "random" if settings.get("randomizeWpotd") == "true" else settings.get("customDate") or "today"
        key = json.dumps([selection, dimensions, resize])
        path = Path(device_config.plugin_image_dir) / ".wpotd-last.png"
        try:
            url = self._fetch_potd(datetofetch)["image_src"]
            image = self._download_image(url, dimensions, resize)
        except RuntimeError:
            cached = self._read_cached(path, key, dimensions)
            if cached is None:
                raise
            return cached
        self._save_cached(path, key, datetofetch, image, dimensions)
        return image

    def _save_cached(self, path, key, source_date, image, dimensions):
        # One panel-sized fallback across all WPOTD requests. Original-size mode
        # keeps its full-resolution live result. Crop to the panel for the fallback.
        from utils.image_utils import resize_image
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("request", key)
        metadata.add_text("source_date", source_date.isoformat())
        temp_path = None
        try:
            if dimensions[0] * dimensions[1] > 1_000_000:
                return
            cached = resize_image(image, dimensions) if image.size != dimensions else image
            with NamedTemporaryFile(dir=path.parent, prefix=".wpotd-", suffix=".tmp", delete=False) as out:
                temp_path = Path(out.name)
                cached.save(out, format="PNG", pnginfo=metadata)
                length = out.tell()
            if length <= self.CACHE_LIMIT:
                os.replace(temp_path, path)
        except (OSError, ValueError):
            logger.warning("Could not save the WPOTD fallback")
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _read_cached(self, path, key, dimensions):
        try:
            if path.stat().st_size > self.CACHE_LIMIT:
                return None
            with Image.open(path) as source:
                if source.info.get("request") != key or source.size != dimensions or source.width * source.height > 1_000_000:
                    return None
                stamp = date.fromisoformat(source.info["source_date"])
                cached = source.convert("RGB")
            draw = ImageDraw.Draw(cached)
            label = f"Cached | {stamp.isoformat()}"
            draw.rectangle((0, 0, cached.width - 1, 19), fill="white")
            draw.text((4, 3), label, fill="black")
            return cached
        except (OSError, ValueError, KeyError, Image.DecompressionBombError):
            return None

    def _determine_date(self, settings: Dict[str, Any]) -> date:
        if settings.get("randomizeWpotd") == "true":
            start = datetime(2015, 1, 1)
            delta_days = (datetime.today() - start).days
            return (start + timedelta(days=randint(0, delta_days))).date()
        elif settings.get("customDate"):
            return datetime.strptime(settings["customDate"], "%Y-%m-%d").date()
        else:
            return datetime.today().date()

    def _download_image(self, url: str, dimensions: tuple = None, resize: bool = False) -> Image.Image:
        """
        Download image from URL, optionally resizing with adaptive loader.

        Args:
            url: Image URL
            dimensions: Target dimensions if resizing
            resize: Whether to use adaptive resizing
        """
        if urlsplit(url).path.lower().endswith(self.NON_IMAGE):
            raise RuntimeError("Unsupported Wikipedia picture media type")
        image = self.image_loader.from_url(url, dimensions, timeout_ms=10000, resize=resize, headers=self.HEADERS)
        if image is None:
            raise RuntimeError("Unable to load Wikipedia picture within resource limits")
        return image

    def _fetch_potd(self, cur_date: date) -> Dict[str, Any]:
        title = f"Template:POTD/{cur_date.isoformat()}"
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "images",
            "titles": title
        }

        data = self._make_request(params)
        try:
            filename = data["query"]["pages"][0]["images"][0]["title"]
        except (KeyError, IndexError) as e:
            logger.error(f"Failed to retrieve POTD filename for {cur_date}: {e}")
            raise RuntimeError("Failed to retrieve POTD filename.")

        image_src = self._fetch_image_src(filename)

        return {
            "filename": filename,
            "image_src": image_src,
            "image_page_url": f"https://en.wikipedia.org/wiki/{title}",
            "date": cur_date
        }

    def _fetch_image_src(self, filename: str) -> str:
        params = {
            "action": "query",
            "format": "json",
            "prop": "imageinfo",
            "iiprop": "url",
            "titles": filename
        }
        data = self._make_request(params)
        try:
            page = next(iter(data["query"]["pages"].values()))
            return page["imageinfo"][0]["url"]
        except (KeyError, IndexError, StopIteration) as e:
            logger.error(f"Failed to retrieve image URL for {filename}: {e}")
            raise RuntimeError("Failed to retrieve image URL.")

    def _make_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            session = get_http_session()
            deadline = time.monotonic() + 10
            with session.get(self.API_URL, params=params, headers=self.HEADERS, timeout=(5, 5), stream=True) as response:
                response.raise_for_status()
                return json.loads(b"".join(bounded_chunks(response, 256 * 1024, deadline)))
        except Exception as e:
            logger.error(f"Wikipedia API request failed with params {params}: {str(e)}")
            raise RuntimeError("Wikipedia API request failed.")
