"""Compare frame preparation against another checkout and measure local renderer processes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
from statistics import median
import subprocess
import sys
import time

import psutil


def measure(source, workload):
    # Use a separate interpreter per workload so earlier peaks and imports do not leak.
    env = dict(os.environ, PYTHONPATH=str(source.resolve()))
    command = [sys.executable, __file__, "--worker", workload]
    peak = 0
    with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, text=True) as child:
        root = psutil.Process(child.pid)
        while child.poll() is None:
            rss = 0
            try:
                for proc in [root, *root.children(recursive=True)]:
                    try:
                        rss += proc.memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
            except psutil.NoSuchProcess:
                pass
            peak = max(rss, peak)
            time.sleep(0.01)
        raw = child.stdout.read()
        if child.returncode:
            raise RuntimeError(f"{workload} exited with {child.returncode}")
    result = json.loads(raw)
    result["sampled_tree_rss_mib"] = round(peak / 2**20, 1)
    return result


def benchmark_frame(name):
    from PIL import Image
    from display.display_manager import DisplayManager

    size = (800, 480) if name == "native" else (960, 640)
    img = Image.frombytes("RGB", size, random.Random(41).randbytes(size[0] * size[1] * 3))
    class Config:
        def get_config(self):
            return {"resolution": [800, 480], "orientation": "horizontal" if name == "native" else "vertical"}
    manager = DisplayManager.__new__(DisplayManager)
    manager.device_config, manager.display_type = Config(), "inky"
    times = []
    for _ in range(35):
        start = time.perf_counter()
        frame, _, digest = manager.prepare_frame(img, [])
        times.append((time.perf_counter() - start) * 1000)
    return {"median_ms": round(median(times[5:]), 3), "frame_signature": digest,
            "pixel_sha256": hashlib.sha256(frame.tobytes()).hexdigest()}


def benchmark_renderer(name):
    from PIL import Image, ImageDraw
    from utils.image_utils import take_screenshot_html

    start = time.perf_counter()
    if name == "pillow":
        img = Image.new("RGB", (800, 480), "white")
        draw = ImageDraw.Draw(img)
        draw.text((24, 24), "Milano | 07:35", fill="black", font_size=30)
        draw.text((24, 100), "09:00 Agenda", fill="black", font_size=24)
        draw.text((424, 100), "Meteo | 12 C", fill="black", font_size=24)
    else:
        html = """<style>body{margin:24px;font:30px sans-serif}main{display:flex;gap:220px;font-size:24px}</style>
        <p>Milano | 07:35</p><main><span>09:00 Agenda</span><span>Meteo | 12 C</span></main>"""
        img = take_screenshot_html(html, (800, 480))
    if img is None or img.size != (800, 480) or img.convert("L").getextrema() != (0, 255):
        raise RuntimeError(f"{name} did not render the local card")
    return {"elapsed_ms": round((time.perf_counter() - start) * 1000, 2)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, help="Reference checkout containing src/")
    parser.add_argument("--renderers", action="store_true", help="Also run Pillow and installed Chromium")
    parser.add_argument("--worker", choices=["native", "rotated", "pillow", "chromium"])
    args = parser.parse_args()
    if args.worker:
        run = benchmark_frame if args.worker in ("native", "rotated") else benchmark_renderer
        print(json.dumps(run(args.worker)))
    else:
        src = Path(__file__).resolve().parents[1] / "src"
        report = {}
        for workload in ("native", "rotated"):
            report[workload] = {"candidate": measure(src, workload)}
            if args.baseline:
                previous = measure(args.baseline / "src", workload)
                report[workload]["baseline"] = previous
                for key in ("pixel_sha256", "frame_signature"):
                    if previous[key] != report[workload]["candidate"][key]:
                        raise RuntimeError(f"{workload} changed {key}")
        if args.renderers:
            report["renderers"] = {name: measure(src, name) for name in ("pillow", "chromium")}
        print(json.dumps(report, indent=2))
