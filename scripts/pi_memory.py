"""Probe the disposable InkyPi systemd user scope without starting InkyPi."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


LIMIT = 200 * 2**20
PEAK_BUDGET = 160 * 2**20


def new_scope():
    return "inkypi-qc-" + uuid.uuid4().hex + ".scope"


def scope_command(unit, command):
    return ["systemd-run", "--user", "--scope", "--quiet", "--unit=" + unit,
            "--property=MemoryMax=" + str(LIMIT), "--property=MemoryAccounting=yes", *command]


def mount_path(field):
    # proc escapes spaces, tabs, newlines and literal backslashes as octal.
    return Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), field))


def scope_directory(entry, mountinfo):
    candidates = []
    for line in mountinfo.splitlines():
        fields, separator, fs = line.partition(" - ")
        if separator and fs.split()[0] == "cgroup2":
            root, mount = map(mount_path, fields.split()[3:5])
            if entry.is_relative_to(root):
                candidates.append((root, mount, mount / entry.relative_to(root)))
    for root, mount, directory in sorted(candidates, key=lambda c: len(c[0].parts), reverse=True):
        if directory.is_dir():
            return mount, directory
    raise ValueError("The process's cgroup v2 directory is not visible in this mount namespace")


def controllers(path):
    try:
        return path.read_text().split()
    except OSError:
        return None


def measure_scope(pid, unit, proc=Path("/proc")):
    report = {"status": "unverified", "expected_limit_bytes": LIMIT, "peak_budget_bytes": PEAK_BUDGET}
    try:
        entries = (proc / str(pid) / "cgroup").read_text().splitlines()
        entry = next((Path(line[3:]) for line in entries if line.startswith("0::")), None)
        if entry is None:
            raise ValueError("No unified cgroup v2 entry for this process")
        report["cgroup"] = str(entry)
        if not entry.is_absolute() or ".." in entry.parts or entry.name != unit:
            raise ValueError("Process is outside the expected disposable scope")
        mount, directory = scope_directory(entry, (proc / "self/mountinfo").read_text())
    except (OSError, ValueError) as exc:
        report["measurement_error"] = str(exc)
        return report

    errors = {}
    failures = []
    try:
        limit = (directory / "memory.max").read_text().strip()
        report["limit"] = limit
        if limit == "max" or int(limit) != LIMIT:
            failures.append("The scope does not enforce the requested 200 MiB limit")
    except (OSError, ValueError) as exc:
        errors["memory.max"] = str(exc)
        report["mount_controllers"] = controllers(mount / "cgroup.controllers")
        report["parent_subtree_control"] = controllers(directory.parent / "cgroup.subtree_control")
    try:
        peak = int((directory / "memory.peak").read_text())
        report["peak_bytes"] = peak
        if peak > PEAK_BUDGET:
            failures.append("Scope memory peak exceeded 160 MiB")
    except (OSError, ValueError) as exc:
        errors["memory.peak"] = str(exc)
    try:
        events = dict(line.split() for line in (directory / "memory.events").read_text().splitlines())
        report["events"] = events
        if int(events["oom"]) or int(events["oom_kill"]):
            failures.append("The kernel recorded an out-of-memory event in this scope")
    except (OSError, ValueError, KeyError) as exc:
        errors["memory.events"] = str(exc)
    if errors:
        report["unavailable"] = errors
    if failures:
        report.update(status="failed", failures=failures)
    elif not errors:
        report["status"] = "passed"
    return report


def probe():
    unit = new_scope()
    command = [sys.executable, str(Path(__file__).resolve()), "--inspect", unit]
    try:
        run = subprocess.run(scope_command(unit, command), capture_output=True, text=True, timeout=20)
        if run.returncode:
            return {"status": "unverified", "measurement_error": run.stderr.strip() or
                    f"systemd-run exited with status {run.returncode}"}
        result = json.loads(run.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"status": "unverified", "measurement_error": str(exc)}
    if result["status"] == "passed":
        result["status"] = "ready"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", metavar="UNIT", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(measure_scope(os.getpid(), args.inspect)))
    else:
        result = probe()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["status"] == "ready" else 1)
