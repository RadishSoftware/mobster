"""Deep links and the paste path against tapping and typing, on a real USB iPhone, with no model calls.

Two tests, each driving the phone's WebDriverAgent through Mobster's driver (the Smart loop's own calls) and writing
``<out>/<test>.json``:

    links   5 scripted tasks, each done twice per repetition: the tap path (launch the app, tap or type down to the
            screen) and one OPEN_URL (deeplinks.open_link). Counted: actions and seconds from the first action to
            the target's label on screen. Every link here opens a screen and changes nothing.
    paste   texts of 200, 500 and 1,000 characters into a new Notes note: typed key by key (WDA's typing), and
            pasted (write_text: setPasteboard, then the edit menu's Paste). Counted: seconds and an exact read-back.
            The notes are deleted after.

``--plan`` prints the tasks and their action counts and touches nothing. Nothing opens a window on the Mac.

    python -m mobile_agent.bench.native --wda-url http://127.0.0.1:8100 --out <dir> links paste
"""

import argparse
import json
import pathlib
import statistics
import sys
import time

SETTINGS, MAPS = "com.apple.Preferences", "com.apple.Maps"
# (name, the tap path as (op, label | text), the link, the label that proves the screen arrived)
TASKS = (
    ("Settings › Wi-Fi", [("launch", SETTINGS), ("tap", "Wi-Fi")], "App-prefs:WIFI", "Wi-Fi"),
    ("Settings › Bluetooth", [("launch", SETTINGS), ("tap", "Bluetooth")], "App-prefs:Bluetooth", "Bluetooth"),
    ("Settings › General › About", [("launch", SETTINGS), ("tap", "General"), ("tap", "About")],
     "App-prefs:General&path=About", "About"),
    ("Maps: search coffee", [("launch", MAPS), ("tap", "Search Maps"), ("type_submit", "coffee")],
     "maps://?q=coffee", "coffee"),
    ("App Store: search Notes", [("launch", "com.apple.AppStore"), ("tap", "Search"), ("tap", "Games, Apps and more"),
                                 ("type_submit", "Notes")],
     "itms-apps://search.itunes.apple.com/WebObjects/MZSearch.woa/wa/search?media=software&term=Notes", "Notes"),
)
LENGTHS = (200, 500, 1000)


def plan():
    rows = [{"task": name, "tap_actions": len(path), "link_actions": 1, "link": link} for name, path, link, _ in TASKS]
    return {"tasks": rows, "actions_saved": sum(r["tap_actions"] - 1 for r in rows)}


def _find(snapshot, label):
    wanted = label.casefold()
    for element in snapshot.elements:
        if (element.label or "").casefold() == wanted:
            return element
    for element in snapshot.elements:
        if wanted in (element.label or "").casefold() or wanted in (element.value or "").casefold():
            return element
    return None


def _until(driver, label, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snapshot = driver.observe(timeout=5)
        if _find(snapshot, label) is not None:
            return snapshot
        time.sleep(.15)
    raise TimeoutError(f"{label!r} never showed")


def _home(driver):
    driver.call("POST", "/wda/pressButton", {"name": "home"}, timeout=10)
    time.sleep(1.0)


def _terminate(driver, bundle):
    try:
        driver.call("POST", "/wda/apps/terminate", {"bundleId": bundle}, timeout=15)
    except Exception:
        pass


def run_links(driver, repeats):
    from ..deeplinks import app_for, open_link
    results = []
    for name, path, link, proof in TASKS:
        bundle = next(value for op, value in path if op == "launch")
        taps, links, ok = [], [], {"tap": 0, "link": 0}
        for _ in range(repeats):
            _terminate(driver, bundle)
            _home(driver)
            started = time.monotonic()
            try:
                for op, value in path:
                    if op == "launch":
                        driver.call("POST", "/wda/apps/activate", {"bundleId": value}, timeout=30)
                        snapshot = driver.observe_ready(timeout=10)
                    elif op == "tap":
                        snapshot = _until(driver, value, 10)
                        driver.execute("TAP", _find(snapshot, value), snapshot)
                    else:
                        snapshot = driver.observe(timeout=5)
                        field = next(e for e in snapshot.elements if e.editable)
                        driver.execute("TYPE_SUBMIT", field, snapshot, text=value)
                _until(driver, proof)
                taps.append(time.monotonic() - started)
                ok["tap"] += 1
            except Exception as error:
                print(f"{name}: the tap path failed ({type(error).__name__}: {error})", file=sys.stderr)
            _terminate(driver, bundle)
            _home(driver)
            started = time.monotonic()
            try:
                open_link(driver, link, bundle=app_for(link) or None)
                _until(driver, proof)
                links.append(time.monotonic() - started)
                ok["link"] += 1
            except Exception as error:
                print(f"{name}: the link failed ({type(error).__name__}: {error})", file=sys.stderr)
        results.append({"task": name, "tap_actions": len(path), "link_actions": 1, "ok": ok,
                        "tap_s_p50": round(statistics.median(taps), 2) if taps else None,
                        "link_s_p50": round(statistics.median(links), 2) if links else None})
    return results


def run_paste(driver, repeats):
    from .actuation import bodies
    notes = "com.apple.mobilenotes"
    out = []
    for length in LENGTHS:
        timings = {"type": [], "paste": []}
        exact = {"type": 0, "paste": 0}
        for method in ("type", "paste"):
            for text in bodies(repeats, chars=length, seed=length):
                text = text.replace("\n", " ")
                driver.call("POST", "/wda/apps/activate", {"bundleId": notes}, timeout=30)
                snapshot = driver.observe_ready(timeout=10)
                compose = _find(snapshot, "New Note")
                driver.execute("TAP", compose, snapshot)
                time.sleep(1.0)
                snapshot = driver.observe(timeout=5)
                field = next(e for e in snapshot.elements if e.editable and e.role == "TextView")
                saved = driver.paste_min_chars
                driver.paste_min_chars = 10 ** 9 if method == "type" else 1
                started = time.monotonic()
                try:
                    receipt = driver.write_text(field, snapshot, text, timeout=120)
                finally:
                    driver.paste_min_chars = saved
                timings[method].append(time.monotonic() - started)
                exact[method] += bool(receipt.get("matches"))
                _delete_note(driver)
        out.append({"chars": length, "exact": exact,
                    "type_s_p50": round(statistics.median(timings["type"]), 2),
                    "paste_s_p50": round(statistics.median(timings["paste"]), 2)})
    return out


def _delete_note(driver):
    """The note just written goes to Recently Deleted: Done, back, then swipe its row and Delete."""
    for label in ("Done", "Notes", "Back"):
        snapshot = driver.observe(timeout=5)
        element = _find(snapshot, label)
        if element is not None and element.role == "Button":
            driver.execute("TAP", element, snapshot)
            time.sleep(.8)
    snapshot = driver.observe(timeout=5)
    rows = [e for e in snapshot.elements if e.role == "Cell"]
    if rows:
        driver.execute("SWIPE_LEFT", rows[0], snapshot)
        time.sleep(.6)
        snapshot = driver.observe(timeout=5)
        delete = _find(snapshot, "Delete")
        if delete is not None:
            driver.execute("TAP", delete, snapshot)
            time.sleep(.6)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tests", nargs="*", metavar="TEST", help="links, paste")
    parser.add_argument("--wda-url", default="http://127.0.0.1:8100")
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("native-bench"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--plan", action="store_true", help="print the tasks and action counts; touch nothing")
    args = parser.parse_args(argv)
    unknown = [test for test in args.tests if test not in ("links", "paste")]
    if unknown:
        parser.error(f"unknown test: {', '.join(unknown)}")
    if args.plan or not args.tests:
        print(json.dumps(plan(), indent=2, ensure_ascii=False))
        return 0
    from ..drivers import WDA
    driver = WDA(args.wda_url)
    driver.configure()
    args.out.mkdir(parents=True, exist_ok=True)
    for test in args.tests:
        result = run_links(driver, args.repeats) if test == "links" else run_paste(driver, args.repeats)
        (args.out / f"{test}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
