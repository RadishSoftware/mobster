"""Render the terminal UI headlessly in its main states, as SVG and optionally PNG.

    python scripts/tui_screenshots.py OUT_DIR [--size 120x36 --size 80x24] [--png]

Runs the real app with Textual's pilot over the scripted demo phone (no iPhone,
keys or network), plus a runtime pointed at a closed port for the no-phone
states. --png converts each SVG with headless Google Chrome, which opens no window.
docs/images/ comes from the "hero" shots at 116x34 and the 120x36 set.
"""

import argparse
import asyncio
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["MOBSTER_ASK_BEFORE_ACTING"] = "1"
os.environ["MOBSTER_BYPASS_CHECKS"] = "0"

from mobile_agent.tui.app import MobsterApp  # noqa: E402
from mobile_agent.tui.session import Session  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MESSAGE_TASK = "Text Alex “Running 10 minutes late, save me a seat”"


async def until(pilot, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(.05)
        if predicate():
            return
    raise TimeoutError("the UI never reached the expected state")


async def submit(pilot, text):
    pilot.app.query_one("#prompt").value = text
    await pilot.pause(.05)
    await pilot.press("enter")


def demo_app():
    return MobsterApp(lambda: Session(demo=True, pace=.6), demo=True, bell=False)


def finished(app):
    return app.running is None and app.run_view is not None and app.run_view.narrator.status != "running"


async def states(out, size):
    tag = f"{size[0]}x{size[1]}"

    def shot(app, name):
        print(app.save_screenshot(filename=f"{name}-{tag}.svg", path=str(out)))

    app = demo_app()
    async with app.run_test(size=size) as pilot:
        await until(pilot, lambda: app.session is not None)
        await pilot.pause(.4)
        shot(app, "01-idle")
        await submit(pilot, "Turn on Dark Mode in Settings")
        await until(pilot, lambda: app.run_view is not None and app.run_view.steps >= 2 and app.running is not None
                    and app.run_view.narrator.step.state in {"thinking", "acting"})
        await pilot.pause(.25)
        shot(app, "02-running")
        await until(pilot, lambda: finished(app))
        await pilot.pause(.6)
        shot(app, "03-finished")
        await submit(pilot, MESSAGE_TASK)
        await until(pilot, lambda: app.query_one("#approval").has_class("open"))
        await pilot.pause(1.6)
        shot(app, "04-approval")
        await pilot.press("y")
        await until(pilot, lambda: finished(app))
        await pilot.pause(.6)
        shot(app, "05-approved")
        await pilot.press("ctrl+o")
        await pilot.pause(.3)
        shot(app, "06-details")
        await pilot.press("ctrl+o")
        await submit(pilot, "Pay my card bill in Wallet")
        await until(pilot, lambda: finished(app) and app.run_view.run.app["id"] == "wallet")
        await pilot.pause(.6)
        shot(app, "07-blocked")
        prompt = app.query_one("#prompt")
        prompt.value = "/"
        await pilot.pause(.3)
        shot(app, "08-commands")
        prompt.value = "@mes"
        await pilot.pause(.3)
        shot(app, "09-app-mention")
        prompt.value = ""
        await pilot.press("ctrl+r")
        await pilot.pause(.4)
        shot(app, "10-history")
        await pilot.press("escape")
        await pilot.pause(.2)
        await pilot.press("question_mark")
        await pilot.pause(.3)
        shot(app, "11-help")
        await pilot.press("escape")

    history = Path(tempfile.mkdtemp()) / "terminal.sqlite3"
    app = MobsterApp(lambda: Session(wda_url="http://127.0.0.1:9", history_db=history), bell=False)
    async with app.run_test(size=size) as pilot:
        await until(pilot, lambda: bool(app.status))
        await pilot.pause(.3)
        shot(app, "12-no-phone")
        await submit(pilot, "Turn on Dark Mode")
        await pilot.pause(1)
        shot(app, "13-not-started")


async def hero(out):
    app = MobsterApp(lambda: Session(demo=True, pace=.6), demo=True, bell=False)
    async with app.run_test(size=(116, 34)) as pilot:
        await until(pilot, lambda: app.session is not None)
        await submit(pilot, MESSAGE_TASK)
        await until(pilot, lambda: app.query_one("#approval").has_class("open"))
        await pilot.pause(1.6)
        print(app.save_screenshot(filename="hero-approval.svg", path=str(out)))
        await pilot.press("y")
        await until(pilot, lambda: finished(app))
        await submit(pilot, "Turn on Dark Mode")
        await until(pilot, lambda: finished(app) and app.run_view.run.app["id"] == "settings")
        await pilot.pause(1.8)
        print(app.save_screenshot(filename="hero-finished.svg", path=str(out)))


def to_png(svg_path, scale=2):
    """An SVG screenshot as PNG, rendered by headless Chrome (it keeps Textual's fonts and layout)."""
    svg = svg_path.read_text(encoding="utf-8")
    match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    width, height = (float(match[1]), float(match[2])) if match else (1200, 800)
    page = Path(tempfile.mkdtemp()) / "shot.html"
    page.write_text("<!doctype html><style>html,body{margin:0;background:#0c0c0c}svg{display:block;"
                    f"width:{width}px;height:{height}px}}</style>{svg}", encoding="utf-8")
    png = svg_path.with_suffix(".png")
    png.unlink(missing_ok=True)
    chrome = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                               f"--user-data-dir={tempfile.mkdtemp()}", f"--force-device-scale-factor={scale}",
                               f"--window-size={int(width)},{int(height)}", f"--screenshot={png}", page.as_uri()],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Chrome writes the file and sometimes lingers: stop it once the file stops growing.
    deadline, last = time.monotonic() + 60, -1
    while time.monotonic() < deadline and chrome.poll() is None:
        time.sleep(.5)
        if png.exists() and png.stat().st_size == last > 0:
            break
        last = png.stat().st_size if png.exists() else -1
    chrome.kill()
    chrome.wait()
    return png


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--size", action="append", help="COLUMNSxROWS (default: 120x36 and 80x24)")
    parser.add_argument("--png", action="store_true", help="also write PNGs with headless Google Chrome")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    sizes = [tuple(map(int, size.split("x"))) for size in (args.size or ["120x36", "80x24"])]

    async def run():
        await hero(args.out)
        for size in sizes:
            await states(args.out, size)
    asyncio.run(run())
    if args.png:
        for svg in sorted(args.out.glob("*.svg")):
            print(to_png(svg))


if __name__ == "__main__":
    main()
