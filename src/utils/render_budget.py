"""Consult the active Linux cgroup while a browser render is in progress."""
from pathlib import Path


def memory_limits(mount=Path("/sys/fs/cgroup"), membership=Path("/proc/self/cgroup")):
    """Return finite cgroup v2 limits that apply to this process."""
    limits = []
    try:
        group = next(line[3:] for line in membership.read_text().splitlines() if line.startswith("0::"))
        base = mount.resolve()
        directory = (base / group.lstrip("/")).resolve()
        if not directory.is_relative_to(base) or not directory.is_dir():
            # A container can mount its own cgroup as the hierarchy root.
            directory = base
        while True:
            maximum = directory / "memory.max"
            if maximum.is_file():
                value = maximum.read_text().strip()
                if value != "max":
                    limits.append((directory, int(value)))
            if directory == base:
                break
            directory = directory.parent
    except (OSError, ValueError, StopIteration):
        pass
    return limits


def check_headroom(limits):
    for group, limit in limits:
        try:
            current = int((group / "memory.current").read_text())
            # Inactive file pages are reclaimable. Count the working set to
            # avoid refusing a render simply because the SD-card cache is warm.
            fields = dict(line.split() for line in (group / "memory.stat").read_text().splitlines())
            working = max(0, current - int(fields.get("inactive_file", 0)))
        except (OSError, ValueError):
            continue
        reserve = min(24 * 1024 * 1024, limit // 8)
        if working >= limit - reserve:
            raise MemoryError("Browser render stopped near the service memory limit")
