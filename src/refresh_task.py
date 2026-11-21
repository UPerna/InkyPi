import threading
import time
import os
import logging
import psutil
import pytz
from collections import OrderedDict
from concurrent.futures import Future
from datetime import datetime
from uuid import uuid4
from plugins.plugin_registry import get_plugin_instance
from utils.app_utils import generate_startup_image
from model import RefreshInfo, PlaylistManager
from PIL import Image

logger = logging.getLogger(__name__)

class RefreshTask:
    """Handles the logic for refreshing the display using a background thread."""

    def __init__(self, device_config, display_manager):
        self.device_config = device_config
        self.display_manager = display_manager

        self.thread = None
        self.condition = threading.Condition()
        self.running = False
        self.pending = None
        self.active_id = None
        self.history = OrderedDict()
        self.config_changed = False

    def start(self):
        with self.condition:
            if self.thread and self.thread.is_alive():
                return
            self.running = True
            self.thread = threading.Thread(target=self._run, name="display-refresh", daemon=True)
            self.thread.start()

    def stop(self):
        with self.condition:
            self.running = False
            pending = self.pending
            if pending:
                result = self._record(pending, "failed", "Refresh worker stopped.")
                self.pending = None
            self.condition.notify_all()
        if pending:
            pending.future.set_result(result)
        if self.thread:
            self.thread.join()

    def _record(self, job, state, message):
        # Called with the condition held. Retain only public, settings-free outcomes.
        result = {"id": job.id, "state": state, "message": message}
        self.history[job.id] = result
        if len(self.history) > 128:
            self.history.popitem(last=False)
        return result

    def submit(self, action, force_display=False):
        with self.condition:
            if not self.running:
                raise RuntimeError("Refresh worker is stopped.")
            displaced = self.pending
            if displaced:
                result = self._record(displaced, "superseded", "Replaced by a newer refresh.")
            job = RefreshRequest(action, force_display)
            self.pending = job
            self.condition.notify()
        if displaced:
            displaced.future.set_result(result)
        return job

    def manual_update(self, refresh_action, timeout=300):
        """Synchronous compatibility entry point for callers outside the web UI."""
        result = self.submit(refresh_action).future.result(timeout=timeout)
        if result["state"] != "completed":
            raise RuntimeError(result["message"])

    def status(self, request_id):
        with self.condition:
            if self.active_id == request_id:
                return {"id": request_id, "state": "running", "message": "Refreshing display."}
            if self.pending and self.pending.id == request_id:
                return {"id": request_id, "state": "queued", "message": "Refresh queued."}
            return self.history.get(request_id)

    def signal_config_change(self):
        with self.condition:
            self.config_changed = True
            self.condition.notify()

    def _interval(self):
        return max(1, int(self.device_config.get_config("plugin_cycle_interval_seconds", default=3600)))

    def _remaining_interval(self):
        interval = self._interval()
        last = self.device_config.get_refresh_info().get_refresh_datetime()
        if last is None:
            return interval
        elapsed = (self._get_current_datetime() - last).total_seconds()
        return max(0, interval - max(0, elapsed))

    def _run(self):
        try:
            if self.device_config.get_config("startup") is True:
                image = generate_startup_image(self.device_config.get_resolution())
                digest, _ = self.display_manager.display_image(image, force=True)
                info = RefreshInfo("Startup", None, self._get_current_datetime().isoformat(), digest)
                self.device_config.update_config({"startup": False}, refresh_info=info)
        except Exception:
            logger.exception("Startup frame failed")

        try:
            deadline = time.monotonic() + self._remaining_interval()
        except (TypeError, ValueError, OverflowError):
            logger.exception("Invalid saved refresh time or interval")
            deadline = time.monotonic() + 60
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.config_changed or not self.running,
                                        timeout=max(0, deadline - time.monotonic()))
                if not self.running:
                    return
                job = self.pending
                self.pending = None
                changed = self.config_changed
                self.config_changed = False
                self.active_id = job.id if job else None

            try:
                current_dt = self._get_current_datetime()
                if job:
                    action = job.action
                else:
                    if changed:
                        deadline = time.monotonic() + self._remaining_interval()
                        if deadline > time.monotonic():
                            continue
                    if self.device_config.get_config("log_system_stats"):
                        self.log_system_stats()
                    playlist, instance = self._determine_next_plugin(
                        self.device_config.get_playlist_manager(), self.device_config.get_refresh_info(), current_dt)
                    action = PlaylistRefresh(playlist, instance) if instance else None
                if action:
                    displayed = self._perform(action, current_dt, force_display=bool(job and job.force_display))
                    message = "Display updated." if displayed else "Image already displayed."
                delay = self._remaining_interval() if action else self._interval()
                deadline = time.monotonic() + delay
                state = "completed"
            except Exception:
                logger.exception("Refresh failed")
                state, message = "failed", "Refresh failed. Check the device log and retry."
                # A failed or overdue scheduled item must not create a busy loop.
                if not job or changed:
                    try:
                        deadline = time.monotonic() + self._interval()
                    except (TypeError, ValueError, OverflowError):
                        deadline = time.monotonic() + 60
            finally:
                if job:
                    with self.condition:
                        result = self._record(job, state, message)
                        self.active_id = None
                    job.future.set_result(result)

    def _perform(self, action, current_dt, force_display=False):
        plugin_cfg = self.device_config.get_plugin(action.get_plugin_id())
        if plugin_cfg is None:
            raise ValueError("Requested plugin no longer exists")
        plugin = get_plugin_instance(plugin_cfg)
        image = action.execute(plugin, self.device_config, current_dt)
        digest, displayed = self.display_manager.display_image(
            image, plugin.config.get("image_settings", []), force=force_display)
        info = RefreshInfo(**action.get_refresh_info(), refresh_time=current_dt.isoformat(), image_hash=digest)
        if isinstance(action, PlaylistRefresh) and not action.force and action.plugin_instance in action.playlist.plugins:
            playlist = action.playlist
            previous = playlist.current_plugin_index
            playlist.current_plugin_index = playlist.plugins.index(action.plugin_instance)
            try:
                self.device_config.update_config({}, refresh_info=info)
            except Exception:
                playlist.current_plugin_index = previous
                raise
        else:
            self.device_config.update_config({}, refresh_info=info)
        return displayed

    def _get_current_datetime(self):
        """Retrieves the current datetime based on the device's configured timezone."""
        tz_str = self.device_config.get_config("timezone", default="UTC")
        try:
            tz = pytz.timezone(tz_str)
        except pytz.UnknownTimeZoneError:
            logger.warning("Unknown device timezone, using UTC")
            tz = pytz.UTC
        return datetime.now(tz)

    def _determine_next_plugin(self, playlist_manager, latest_refresh_info, current_dt):
        """Determines the next plugin to refresh based on the active playlist, plugin cycle interval, and current time."""
        playlist = playlist_manager.determine_active_playlist(current_dt)
        if not playlist:
            playlist_manager.active_playlist = None
            logger.info(f"No active playlist determined.")
            return None, None

        playlist_manager.active_playlist = playlist.name
        if not playlist.plugins:
            logger.info(f"Active playlist '{playlist.name}' has no plugins.")
            return None, None

        latest_refresh_dt = latest_refresh_info.get_refresh_datetime()
        plugin_cycle_interval = self.device_config.get_config("plugin_cycle_interval_seconds", default=3600)
        should_refresh = PlaylistManager.should_refresh(latest_refresh_dt, plugin_cycle_interval, current_dt)

        if not should_refresh:
            latest_refresh_str = latest_refresh_dt.strftime('%Y-%m-%d %H:%M:%S') if latest_refresh_dt else "None"
            logger.info(f"Not time to update display. | latest_update: {latest_refresh_str} | plugin_cycle_interval: {plugin_cycle_interval}")
            return None, None

        plugin = playlist.plugins[playlist.next_plugin_index()]
        logger.info(f"Determined next plugin. | active_playlist: {playlist.name} | plugin_instance: {plugin.name}")

        return playlist, plugin
    
    def log_system_stats(self):
        metrics = {
            'cpu_percent': psutil.cpu_percent(interval=1),
            'memory_percent': psutil.virtual_memory().percent,
            'disk_percent': psutil.disk_usage('/').percent,
            'load_avg_1_5_15': os.getloadavg(),
            'swap_percent': psutil.swap_memory().percent,
            'net_io': {
                'bytes_sent': psutil.net_io_counters().bytes_sent,
                'bytes_recv': psutil.net_io_counters().bytes_recv
            }
        }

        logger.info(f"System Stats: {metrics}")

class RefreshRequest:
    def __init__(self, action, force_display):
        self.id = uuid4().hex
        self.action = action
        self.force_display = force_display
        self.future = Future()


class RefreshAction:
    """Base class for a refresh action. Subclasses should override the methods below."""
    
    def refresh(self, plugin, device_config, current_dt):
        """Perform a refresh operation and return the updated image."""
        raise NotImplementedError("Subclasses must implement the refresh method.")
    
    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        raise NotImplementedError("Subclasses must implement the get_refresh_info method.")
    
    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        raise NotImplementedError("Subclasses must implement the get_plugin_id method.")

class ManualRefresh(RefreshAction):
    """Performs a manual refresh based on a plugin's ID and its associated settings.
    
    Attributes:
        plugin_id (str): The ID of the plugin to refresh.
        plugin_settings (dict): The settings for the manual refresh.
    """

    def __init__(self, plugin_id: str, plugin_settings: dict):
        self.plugin_id = plugin_id
        self.plugin_settings = plugin_settings

    def execute(self, plugin, device_config, current_dt: datetime):
        """Performs a manual refresh using the stored plugin ID and settings."""
        return plugin.generate_image(self.plugin_settings, device_config)

    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        return {"refresh_type": "Manual Update", "plugin_id": self.plugin_id}

    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        return self.plugin_id

class PlaylistRefresh(RefreshAction):
    """Performs a refresh using a plugin instance within a playlist context.

    Attributes:
        playlist: The playlist object associated with the refresh.
        plugin_instance: The plugin instance to refresh.
    """

    def __init__(self, playlist, plugin_instance, force=False):
        self.playlist = playlist
        self.plugin_instance = plugin_instance
        self.force = force

    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        return {
            "refresh_type": "Playlist",
            "playlist": self.playlist.name,
            "plugin_id": self.plugin_instance.plugin_id,
            "plugin_instance": self.plugin_instance.name
        }

    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        return self.plugin_instance.plugin_id

    def execute(self, plugin, device_config, current_dt: datetime):
        """Performs a refresh for the specified plugin instance within its playlist context."""
        # Determine the file path for the plugin's image
        plugin_image_path = os.path.join(device_config.plugin_image_dir, self.plugin_instance.get_image_path())

        # Check if a refresh is needed based on the plugin instance's criteria
        if self.plugin_instance.should_refresh(current_dt) or self.force:
            logger.info(f"Refreshing plugin instance. | plugin_instance: '{self.plugin_instance.name}'") 
            # Generate a new image
            image = plugin.generate_image(self.plugin_instance.settings, device_config)
            image.save(plugin_image_path)
            self.plugin_instance.latest_refresh_time = current_dt.isoformat()
        else:
            logger.info(f"Not time to refresh plugin instance, using latest image. | plugin_instance: {self.plugin_instance.name}.")
            # Load the existing image from disk
            with Image.open(plugin_image_path) as img:
                image = img.copy()

        return image
