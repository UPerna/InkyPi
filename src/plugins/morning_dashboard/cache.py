"""Private, per-source snapshots for the morning display."""
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile

logger = logging.getLogger(__name__)
FRESH_FOR = timedelta(minutes=15)


def snapshot(directory, source, now, fetch):
    name = hashlib.sha256(json.dumps(source, ensure_ascii=False).encode()).hexdigest()
    path = Path(directory) / (name + ".json")
    old = stamp = None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(saved["at"])
        if stamp.utcoffset() is not None and stamp <= now:
            old = saved["data"]
            if isinstance(old, dict) and now - stamp < FRESH_FOR:
                return old, stamp, False
            if not isinstance(old, dict):
                old = stamp = None
        else:
            stamp = None
    except (OSError, ValueError, KeyError, TypeError):
        pass

    try:
        data = fetch()
    except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError) as exc:
        # Exception messages from providers may contain private feed URLs.
        logger.warning("Dashboard source unavailable (%s)", type(exc).__name__)
        return old, stamp, True

    temp = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8",
                                         delete=False) as stream:
            temp = Path(stream.name)
            json.dump({"at": now.isoformat(), "data": data}, stream, ensure_ascii=False)
        os.replace(temp, path)
    except OSError as exc:
        logger.warning("Dashboard snapshot could not be saved (%s)", type(exc).__name__)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return data, now, False
