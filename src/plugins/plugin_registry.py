"""Discover configured plugins at startup and construct each one on first use."""
import importlib
from pathlib import Path
from threading import RLock

from utils.app_utils import resolve_path

PLUGINS_DIR = "plugins"
PLUGIN_CLASSES = {}
_registered = {}
_registry_lock = RLock()


def load_plugins(plugins_config):
    """Register plugin metadata without importing unused optional dependencies."""
    if isinstance(plugins_config, dict):
        plugins_config = [plugins_config]
    root = Path(resolve_path(PLUGINS_DIR))
    with _registry_lock:
        for plugin in plugins_config:
            plugin_id = plugin["id"]
            path = root / plugin_id / f"{plugin_id}.py"
            if plugin.get("disabled") or not path.is_file():
                _registered.pop(plugin_id, None)
                PLUGIN_CLASSES.pop(plugin_id, None)
                continue
            if _registered.get(plugin_id) != plugin:
                PLUGIN_CLASSES.pop(plugin_id, None)
            _registered[plugin_id] = plugin


def get_plugin_instance(plugin_config):
    plugin_id = plugin_config["id"]
    with _registry_lock:
        if plugin_id in PLUGIN_CLASSES:
            return PLUGIN_CLASSES[plugin_id]
        if plugin_id not in _registered:
            raise ValueError(f"Plugin '{plugin_id}' is not registered.")
        config = _registered[plugin_id]
        try:
            module = importlib.import_module(f"plugins.{plugin_id}.{plugin_id}")
            plugin_class = getattr(module, config["class"])
        except (ImportError, AttributeError) as exc:
            raise ValueError(f"Plugin '{plugin_id}' could not be loaded.") from exc
        instance = plugin_class(config)
        PLUGIN_CLASSES[plugin_id] = instance
        return instance
