"""Measure a disposable, loopback-only InkyPi service with its real refresh worker."""
import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time

from PIL import Image
import psutil
import requests


def serve(root):
    from waitress import create_server
    sys.path.insert(0, str(root / "src"))
    sys.argv = ["inkypi.py", "--dev"]
    import inkypi

    server = create_server(inkypi.app, host="127.0.0.1", port=0, threads=1)
    inkypi.refresh_task.start()
    (root / "port.tmp").write_text(str(server.effective_port))
    (root / "port.tmp").replace(root / "port")
    try:
        server.run()
    finally:
        inkypi.refresh_task.stop()


def memory(service):
    rss = pss = processes = 0
    for child in [service, *service.children(recursive=True)]:
        try:
            info = child.memory_full_info()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        rss += info.rss
        pss += info.pss
        processes += 1
    return {"rss_mib": round(rss / 2**20, 1), "pss_mib": round(pss / 2**20, 1),
            "processes": processes}


def refresh(session, url, service, form):
    start = time.monotonic()
    response = session.post(url + "/update_now", data=form, timeout=5)
    assert response.status_code == 202, response.text
    peak = memory(service)
    while time.monotonic() - start < 120:
        current = memory(service)
        peak = {key: max(peak[key], value) for key, value in current.items()}
        result = session.get(url + response.json()["status_url"], timeout=5).json()
        if result["state"] == "completed":
            png = session.get(url + "/api/current_image", timeout=5)
            png.raise_for_status()
            with Image.open(BytesIO(png.content)) as frame:
                assert frame.size == (800, 480)
                assert frame.convert("L").getextrema() == (0, 255)
            return {"sampled_peak": peak, "elapsed_ms": round((time.monotonic() - start) * 1000, 1)}
        if result["state"] != "running" and result["state"] != "queued":
            raise RuntimeError(result["message"])
        time.sleep(0.03)
    raise TimeoutError("Service refresh did not finish")


def run_checkout(checkout, with_browser):
    with tempfile.TemporaryDirectory(prefix="inkypi-service-qc-") as temporary:
        root = Path(temporary)
        archive = root / "src.tar"
        subprocess.run(["git", "-C", str(checkout), "archive", "-o", str(archive), "HEAD", "src"], check=True)
        with tarfile.open(archive) as stream:
            stream.extractall(root, filter="data")
        page = root / "card.html"
        page.write_text("<style>body{font:30px sans-serif}</style><p>Milano | 07:35</p><p>09:00 Agenda | Meteo 12 C</p>")
        env = dict(os.environ, SRC_DIR=str(root / "src"), PYTHONPATH=str(root / "src"))
        with (root / "service.log").open("w") as log, requests.Session() as session:
            session.trust_env = False
            start = time.monotonic()
            with subprocess.Popen([sys.executable, __file__, "--serve", str(root)], cwd=root,
                                  env=env, stdout=log, stderr=subprocess.STDOUT) as child:
                try:
                    while not (root / "port").exists():
                        if child.poll() is not None or time.monotonic() - start > 60:
                            raise RuntimeError("Mock service did not start")
                        time.sleep(0.02)
                    url = "http://127.0.0.1:" + (root / "port").read_text()
                    service = psutil.Process(child.pid)
                    result = {"startup_ms": round((time.monotonic() - start) * 1000, 1), "idle": memory(service)}
                    result["clock"] = refresh(session, url, service, {"plugin_id": "clock", "selectedClockFace": "Digital Clock"})
                    if with_browser:
                        result["screenshot"] = refresh(session, url, service, {"plugin_id": "screenshot", "url": page.as_uri()})
                    return result
                except Exception:
                    print((root / "service.log").read_text()[-1800:], file=sys.stderr)
                    raise
                finally:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--serve", type=Path)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
    else:
        report = {"candidate": run_checkout(Path(__file__).resolve().parents[1], True)}
        if args.baseline:
            report["baseline"] = run_checkout(args.baseline, False)
        print(json.dumps(report, indent=2))
