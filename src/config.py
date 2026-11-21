import os
import json
import logging
from pathlib import Path
import stat
import tempfile
import threading
from dotenv import load_dotenv
from model import PlaylistManager, RefreshInfo

logger = logging.getLogger(__name__)


def _decode_config(raw):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Device configuration must be a JSON object")
    # Older installs can omit either model. Check that the service can load
    # the models before publishing a candidate or accepting a recovery copy.
    try:
        PlaylistManager.from_dict(data.get("playlist_config", {}))
        RefreshInfo.from_dict(data.get("refresh_info", {}))
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("Invalid playlist or refresh configuration") from exc
    return data


def _replace_file(path, content, metadata):
    """Publish complete bytes on the same filesystem, preserving Pi ownership and mode."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    temp_path = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            if os.name == "posix":
                current = os.fstat(stream.fileno())
                if (current.st_uid, current.st_gid) != (metadata.st_uid, metadata.st_gid):
                    os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Config:
    # Base path for the project directory
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

    # File paths relative to the script's directory
    config_file = os.path.join(BASE_DIR, "config", "device.json")

    # File path for storing the current image being displayed
    current_image_file = os.path.join(BASE_DIR, "static", "images", "current_image.png")

    # Directory path for storing plugin instance images
    plugin_image_dir = os.path.join(BASE_DIR, "static", "images", "plugins")

    def __init__(self):
        self._write_lock = threading.Lock()
        self.config = self.read_config()
        self.plugins_list = self.read_plugins_list()
        self.playlist_manager = self.load_playlist_manager()
        self.refresh_info = self.load_refresh_info()

    def read_config(self):
        """Load a usable configuration, restoring the previous copy if necessary."""
        path = Path(self.config_file).resolve()
        backup = path.with_suffix(path.suffix + ".bak")
        with self._write_lock:
            try:
                raw = path.read_bytes()
                data = _decode_config(raw)
            except (FileNotFoundError, ValueError) as primary_error:
                try:
                    raw = backup.read_bytes()
                    data = _decode_config(raw)
                except (FileNotFoundError, ValueError) as backup_error:
                    raise ValueError("No usable primary or backup device configuration") from backup_error
                try:
                    metadata = path.stat()
                except FileNotFoundError:
                    metadata = backup.stat()
                _replace_file(path, raw, metadata)
                _sync_directory(path.parent)
                logger.warning("Restored device configuration from %s (%s)",
                               backup.name, type(primary_error).__name__)
            self._last_saved = raw
            self._source_metadata = path.stat()
            return data

    def read_plugins_list(self):
        """Reads the plugin-info.json config JSON from each plugin folder. Excludes the base plugin."""
        # Iterate over all plugin folders
        plugins_list = []
        for plugin in sorted(os.listdir(os.path.join(self.BASE_DIR, "plugins"))):
            plugin_path = os.path.join(self.BASE_DIR, "plugins", plugin)
            if os.path.isdir(plugin_path) and plugin != "__pycache__":
                # Check if the plugin-info.json file exists
                plugin_info_file = os.path.join(plugin_path, "plugin-info.json")
                if os.path.isfile(plugin_info_file):
                    logger.debug(f"Reading plugin info from {plugin_info_file}")
                    with open(plugin_info_file) as f:
                        plugin_info = json.load(f)
                    plugins_list.append(plugin_info)

        return plugins_list

    def write_config(self):
        """Save the current model state, including pending in-memory changes."""
        self.update_config({})

    def get_config(self, key=None, default={}):
        """Gets the value of a specific configuration key or returns the entire config if none provided."""
        if key is not None:
            return self.config.get(key, default)
        return self.config

    def get_plugins(self):
        """Returns the list of plugin configurations, sorted by custom order if set."""
        plugin_order = self.config.get('plugin_order', [])

        if not plugin_order:
            return self.plugins_list

        # Create a dict for quick lookup
        plugins_dict = {p['id']: p for p in self.plugins_list}

        # Build ordered list
        ordered = []
        for plugin_id in plugin_order:
            if plugin_id in plugins_dict:
                ordered.append(plugins_dict.pop(plugin_id))

        # Append any remaining plugins not in the order (new plugins)
        ordered.extend(plugins_dict.values())

        return ordered

    def set_plugin_order(self, order):
        """Sets the custom plugin display order."""
        self.update_value('plugin_order', order, write=True)

    def get_plugin(self, plugin_id):
        """Finds and returns a plugin config by its ID."""
        return next((plugin for plugin in self.plugins_list if plugin['id'] == plugin_id), None)

    def get_resolution(self):
        """Returns the display resolution as a tuple (width, height) from the configuration."""
        resolution = self.get_config("resolution")
        width, height = resolution
        return (int(width), int(height))

    def update_config(self, config, *, refresh_info=None):
        """Serialize saves from web and refresh threads sharing this service instance."""
        path = Path(self.config_file).resolve()
        backup = path.with_suffix(path.suffix + ".bak")
        with self._write_lock:
            latest = self.refresh_info if refresh_info is None else refresh_info
            candidate = {**self.config, **config, "playlist_config": self.playlist_manager.to_dict(),
                         "refresh_info": latest.to_dict()}
            raw = json.dumps(candidate, indent=4).encode("utf-8")
            _decode_config(raw)
            if raw != self._last_saved or not path.exists():
                try:
                    metadata = path.stat()
                except FileNotFoundError:
                    # The service already loaded a valid primary this session.
                    metadata = self._source_metadata
                _replace_file(backup, self._last_saved, metadata)
                _sync_directory(path.parent)
                _replace_file(path, raw, metadata)
                # Once renamed, readers see the new primary even if syncing
                # its directory entry subsequently reports a storage error.
                self._last_saved = raw
                self._source_metadata = metadata
            self.config.update(candidate)
            self.refresh_info = latest
            _sync_directory(path.parent)

    def update_value(self, key, value, write=False):
        """Updates a specific key in the configuration with a new value and optionally writes it to the config file."""
        if write:
            self.update_config({key: value})
        else:
            with self._write_lock:
                self.config[key] = value

    def load_env_key(self, key):
        """Loads an environment variable using dotenv and returns its value."""
        load_dotenv(override=True)
        return os.getenv(key)

    def load_playlist_manager(self):
        """Loads the playlist manager object from the config."""
        playlist_manager = PlaylistManager.from_dict(self.get_config("playlist_config"))
        if not playlist_manager.playlists:
            playlist_manager.add_default_playlist()
        return playlist_manager

    def load_refresh_info(self):
        """Loads the refresh information from the config."""
        return RefreshInfo.from_dict(self.get_config("refresh_info"))

    def get_playlist_manager(self):
        """Returns the playlist manager."""
        return self.playlist_manager

    def get_refresh_info(self):
        """Returns the refresh information."""
        return self.refresh_info
