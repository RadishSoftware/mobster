"""F2: the exact-actuation bench (Bet 2), on one iOSWorld simulator, with no model calls.

Each test drives the simulator's WDA directly through Mobster's driver, as the frontier policy
does (WDA.tune: rich rows, glides everywhere, snapshotMaxChildren), and writes its numbers to
``<out>/<test>.json``:

    multiline  10 bodies of 3-8 lines into CloudSlides' slide body, a Notes note, a Mail draft and a
               CloudDocs document (SET_TEXT through write_text); pass: exact read-back on all 40
    paste      20 bodies of 1,000 characters; pass: 20 of 20 exact, p50 under 3 s (typing for scale)
    typing     maxTypingFrequency at 120, 180 and 240 (and WDA's 60), 20 bodies of 500 characters each,
               typed, read back whole; keep the highest rate that drops no character
    cart       QuickBite with a non-empty cart; pass: view_cart_button listed, a tap opens the cart 5 of 5
    apps       the first screen of every app; pass: the rich rows add at most 2 rows a screen on average
    wda        mem-046's CityRide path (Account, Activity, the list read, Home, Account) 5 times, then 5
               times with the runner killed mid-path; pass: no run ends in an error, recovery under 15 s
    readlist   READ_LIST over 10 lists in 5 apps, XCTest's drag against the glide; pass: the same rows in
               at most half the time

Headless: nothing here opens a window. Apps are reset between repetitions by terminating and
relaunching them (never by iOSWorld's data reset, which the bench does not need).

    python -m mobile_agent.bench.actuation --udid <UDID> --wda-url http://127.0.0.1:8203 \\
        --mjpeg-url http://127.0.0.1:9203 --xctestrun <runner.xctestrun> --out <dir> multiline paste ...
"""

import argparse
import os
import difflib
import json
import pathlib
import random
import re
import statistics
import string
import subprocess
import sys
import time

from ..drivers import WDA, WDA_RICH_SOURCE_PATH, WDA_SOURCE_PATH
from ..frontier import FrontierAgent, screen_rows
from ..signing import runner_bundle_id
from ..state import from_wda
from ..transport import Deadline, TransportError

APP = "com.iosworld.benchmark."
CLOCK = re.compile(r"\b\d{1,2}:\d{2}(\s?[AP]M)?\b")
WORDS = ("agenda review budget launch safety threat model research gaps benchmark quarterly plan owner "
         "deadline Maya Kai Priya invoice $42.50 12:30 PM Friday it's \"final\" draft (v2) notes—ok 7% "
         "follow-up e-mail #general @jordan").split()


def bodies(count, lines=(3, 8), chars=None, seed=0):
    """Deterministic test texts: ``lines`` line counts, or one text of exactly ``chars`` characters."""
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        if chars:
            text = ""
            while len(text) < chars:
                text += rng.choice(WORDS) + (".\n" if rng.random() < .04 else " ")
            out.append(text[:chars - 1] + rng.choice(string.ascii_letters))
        else:
            out.append("\n".join(" ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 6)))
                                 for _ in range(rng.randint(*lines))))
    return out


class Bench:
    def __init__(self, args):
        self.args = args
        self.out = pathlib.Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.driver = self.connect()

    def connect(self):
        from .iosworld import sim_restarter, wda_session
        driver = WDA(self.args.wda_url, wda_session(self.args.wda_url))
        driver.configure()
        driver.restarter = sim_restarter(self.args.udid, self.args.wda_url, self.args.mjpeg_url, self.args.xctestrun,
                                         log_path=str(self.out / "wda-restart.log"))
        driver.new_session = lambda: wda_session(self.args.wda_url)
        if self.args.mjpeg_url:
            # As the iOSWorld harness runs it: FrameClock on (it demotes an app whose glide keeps moving).
            from ..frame_clock import attach_frame_clock
            attach_frame_clock(driver, wda_url=self.args.wda_url, session=driver.prefix.rsplit("/", 1)[-1],
                               mode="on", mjpeg_url=self.args.mjpeg_url, log_path=str(self.out / "frameclock.jsonl"))
        driver.tune()
        return driver

    def save(self, name, data):
        (self.out / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(data.get("summary", data), ensure_ascii=False), flush=True)

    # -- navigation --------------------------------------------------------------------------

    def fresh(self, app):
        """``app`` relaunched at its root, settled."""
        bundle = APP + app
        try:
            self.driver.call("POST", "/wda/apps/terminate", {"bundleId": bundle}, timeout=20)
        except TransportError:
            pass
        snapshot, _ = self.driver.launch(bundle, timeout=30)
        return snapshot

    def tap(self, label, wait=1.2, role=None, prefix=False):
        """Tap the element labelled ``label`` (or starting with it) on the screen now; the screen after."""
        snapshot = self.driver.observe(timeout=15)
        element = self.find(snapshot, label, role, prefix)
        if element is None:
            raise LookupError(f"{label!r} not on screen")
        self.driver.execute("TAP", element, snapshot, timeout=15)
        time.sleep(wait)
        return self.driver.observe_ready(timeout=15)

    @staticmethod
    def find(snapshot, label, role=None, prefix=False):
        return next((e for e in snapshot.elements if (e.label.startswith(label) if prefix else e.label == label)
                     and (role is None or e.role == role)), None)

    def field(self, app):
        """(snapshot, the editable field F2 writes into) for ``app``."""
        if app == "cloudslides":
            home = self.fresh(app)
            # The first deck card (the order follows the last opened; its labels follow its slides).
            card = next(e for e in home.elements if e.role == "Button" and e.rect[3] > .2)
            self.driver.execute("TAP", card, home, timeout=15)
            time.sleep(2)
            snapshot = self.tap("Edit", 2)
            fields = [e for e in snapshot.elements if e.role == "TextField" and e.editable and not e.label]
            return snapshot, fields[1]
        if app == "notes":
            self.fresh(app)
            snapshot = self.tap("Compose", 1.5)
            return snapshot, self.find(snapshot, "notes_body_editor")
        if app == "mail":
            self.fresh(app)
            snapshot = self.tap("compose", 1.5)
            return snapshot, self.find(snapshot, "compose_body_field")
        if app == "clouddocs":
            home = self.fresh(app)
            card = next(e for e in home.elements if e.role == "Button" and e.rect[3] > .2)  # the first document
            self.driver.execute("TAP", card, home, timeout=15)
            time.sleep(2.5)
            snapshot = self.tap("Edit", 2)
            return snapshot, next(e for e in snapshot.elements if e.role == "TextView" and e.editable)
        raise ValueError(app)

    def same_field(self, field):
        snapshot = self.driver.observe(timeout=15)
        return snapshot, self.driver._same_field(snapshot, field, getattr(self.driver, "_write_rank", None)) or field

    def field_text(self, field):
        """The field's whole text now, read independently of write_text (the focused field's value)."""
        snapshot, field = self.same_field(field)
        value = self.driver._active_text(field, snapshot, Deadline(15), focus=True)
        return value

    # -- the tests -------------------------------------------------------------------------------

    def multiline(self):
        rows, texts = [], bodies(10, seed=2)
        for app in ("cloudslides", "notes", "mail", "clouddocs"):
            snapshot, field = self.field(app)
            for index, text in enumerate(texts):
                snapshot, field = self.same_field(field)
                started = time.monotonic()
                try:
                    receipt = self.driver.write_text(field, snapshot, text, timeout=90)
                    error = None
                except Exception as failure:
                    receipt, error = {}, f"{type(failure).__name__}: {failure}"[:200]
                seconds = time.monotonic() - started
                check = None if error else self.field_text(field)
                rows.append({"app": app, "index": index, "lines": text.count("\n") + 1, "chars": len(text),
                             "method": receipt.get("method"), "repaired": receipt.get("repaired"),
                             "receipt_matches": receipt.get("matches"), "exact": check == text,
                             "seconds": round(seconds, 2), "error": error,
                             "got": None if check == text else (check or "")[:200]})
                print(app, index, rows[-1]["exact"], rows[-1]["method"], rows[-1]["seconds"], error or "", flush=True)
        exact = sum(r["exact"] for r in rows)
        self.save("multiline", {"summary": {"test": "multiline", "exact": exact, "of": len(rows),
                                            "pass": exact == len(rows),
                                            "by_app": {a: sum(r["exact"] for r in rows if r["app"] == a)
                                                       for a in dict.fromkeys(r["app"] for r in rows)},
                                            "repaired": sum(bool(r["repaired"]) for r in rows),
                                            "p50_s": statistics.median(r["seconds"] for r in rows)}, "rows": rows})

    def paste(self):
        rows, texts = [], bodies(20, chars=1000, seed=3)
        snapshot, field = self.field("notes")
        for index, text in enumerate(texts):
            snapshot, field = self.same_field(field)
            started = time.monotonic()
            receipt = self.driver.write_text(field, snapshot, text, timeout=120)
            seconds = time.monotonic() - started
            check = self.field_text(field)
            rows.append({"index": index, "method": receipt.get("method"), "repaired": receipt.get("repaired"),
                         "exact": check == text, "seconds": round(seconds, 2)})
            print("paste", index, rows[-1], flush=True)
        typed = []
        for text in texts[:2]:
            snapshot, field = self.same_field(field)
            self.driver._emptied(field, snapshot, Deadline(20))
            snapshot, field = self.same_field(field)
            started = time.monotonic()
            self.driver.execute("TYPE", field, snapshot, text=text, timeout=120)
            typed.append(round(time.monotonic() - started, 2))
        exact, p50 = sum(r["exact"] for r in rows), statistics.median(r["seconds"] for r in rows)
        self.save("paste", {"summary": {"test": "paste", "exact": exact, "of": len(rows), "p50_s": p50,
                                        "max_s": max(r["seconds"] for r in rows),
                                        "pasted": sum(r["method"] == "paste" for r in rows),
                                        "typing_1000_s": typed, "pass": exact == len(rows) and p50 < 3},
                            "rows": rows})

    def typing(self):
        texts = bodies(20, chars=500, seed=4)
        snapshot, field = self.field("notes")
        rates = {}
        for rate in self.args.rates:
            self.driver.call("POST", "/appium/settings", {"settings": {"maxTypingFrequency": rate}}, timeout=10)
            runs = []
            for index, text in enumerate(texts):
                snapshot, field = self.same_field(field)
                self.driver._emptied(field, snapshot, Deadline(20))
                snapshot, field = self.same_field(field)
                started = time.monotonic()
                self.driver.execute("TYPE", field, snapshot, text=text, timeout=120)
                seconds = time.monotonic() - started
                got = self.field_text(field) or ""
                matcher = difflib.SequenceMatcher(None, text, got, autojunk=False)
                dropped = len(text) - sum(block.size for block in matcher.get_matching_blocks())
                runs.append({"index": index, "exact": got == text, "dropped": dropped,
                             "extra": len(got) - (len(text) - dropped), "seconds": round(seconds, 2)})
                print("rate", rate, runs[-1], flush=True)
            rates[rate] = {"exact": sum(r["exact"] for r in runs), "dropped": sum(r["dropped"] for r in runs),
                           "p50_s": statistics.median(r["seconds"] for r in runs),
                           "chars_per_s": round(500 / statistics.median(r["seconds"] for r in runs), 1),
                           "runs": runs}
        # Back to the tuned rate (WDA.tune's) for whatever runs next.
        self.driver.tune()
        clean = [rate for rate, r in rates.items() if r["dropped"] == 0 and r["exact"] == len(texts)]
        self.save("typing", {"summary": {"test": "typing", "keep": max(clean, default=None),
                                         **{str(rate): {k: v for k, v in r.items() if k != "runs"}
                                            for rate, r in rates.items()}},
                             "rates": {str(k): v for k, v in rates.items()}})

    def cart(self):
        rows = []
        for attempt in range(5):
            self.fresh("quickbite")
            snapshot = self.tap("Dumpling Home", 2)
            if self.find(snapshot, "view_cart_button") is None:
                snapshot = self.tap("Add Soup Dumplings", 2, prefix=True)
            aliases, lines = screen_rows(snapshot)
            listed = [line for line in lines if '"view_cart_button"' in line]
            opened = False
            if listed:
                after = self.tap("view_cart_button", 2)
                opened = [word for word in ("Place Order", "Your Cart", "Checkout", "Subtotal")
                          if word in after.text and word not in snapshot.text]
            rows.append({"attempt": attempt, "listed": bool(listed), "row": listed[0] if listed else None,
                         "opened": bool(opened), "cart_words": opened})
            print("cart", rows[-1], flush=True)
        self.save("cart", {"summary": {"test": "cart", "listed": sum(r["listed"] for r in rows),
                                       "opened": sum(r["opened"] for r in rows), "of": len(rows),
                                       "pass": all(r["listed"] and r["opened"] for r in rows)}, "rows": rows})

    def apps(self):
        rows = []
        manifest = json.loads(pathlib.Path(self.args.manifest).read_text())
        for app in sorted(manifest):
            try:
                self.fresh(app)
                time.sleep(1)
                xml = self.driver.call("GET", WDA_RICH_SOURCE_PATH, timeout=30)
            except Exception as error:
                rows.append({"app": app, "error": f"{type(error).__name__}: {error}"[:160]})
                continue
            plain, rich = screen_rows(from_wda(xml))[1], screen_rows(from_wda(xml, rich=True))[1]
            added = [line for line in rich if line not in plain]
            rows.append({"app": app, "plain": len(plain), "rich": len(rich), "added": len(rich) - len(plain),
                         "new_rows": added[:12]})
            print("apps", app, len(plain), len(rich), added[:4], flush=True)
        done = [r for r in rows if "added" in r]
        mean = statistics.mean(r["added"] for r in done) if done else None
        self.save("apps", {"summary": {"test": "apps", "screens": len(done), "mean_added": mean,
                                       "max_added": max((r["added"] for r in done), default=None),
                                       "pass": mean is not None and mean <= 2}, "rows": rows})

    def wda(self):
        runs = []
        for kill in [False] * 5 + [True] * 5:
            runs.append(self.cityride_path(kill))
            print("wda", runs[-1], flush=True)
        recoveries = [ms for run in runs for ms in run["recoveries_ms"]]
        self.save("wda", {"summary": {"test": "wda", "runs": len(runs),
                                      "errors": sum(run["error"] is not None for run in runs),
                                      "natural_crashes": sum(len(r["recoveries_ms"]) for r in runs[:5]),
                                      "recoveries": len(recoveries),
                                      "recovery_ms_max": max(recoveries, default=None),
                                      "recovery_ms_p50": statistics.median(recoveries) if recoveries else None,
                                      "pass": all(r["error"] is None for r in runs)
                                      and all(ms < 15000 for ms in recoveries)}, "runs": runs})

    def cityride_path(self, kill):
        """Account, Activity, the whole list read, Home, Account: each step retried after a recovery."""
        agent = FrontierAgent(self.driver, None, screenshots=False)
        steps = [("launch", None), ("tap", "Account"), ("tap", "Activity"), ("read", None), ("tap", "Home"),
                 ("tap", "Account")]
        run = {"kill": kill, "recoveries_ms": [], "error": None, "steps": 0}
        started = time.monotonic()
        for index, (kind, label) in enumerate(steps):
            if kill and index == 3:
                self.kill_runner()
            for attempt in range(3):
                try:
                    if kind == "launch":
                        self.fresh("cityride")
                    elif kind == "tap":
                        self.tap(label, 1.5, role="Button")
                    else:
                        agent._read_list(self.driver.observe(timeout=15))
                    run["steps"] += 1
                    break
                except Exception as error:
                    t0 = time.monotonic()
                    if attempt < 2 and agent._recover(error):
                        run["recoveries_ms"].append(round((time.monotonic() - t0) * 1000))
                        run.setdefault("phases", []).append({**(self.driver.last_recovery or {}), **getattr(
                            self.driver.restarter, "timing", {})})
                        continue
                    run["error"] = f"{type(error).__name__}: {error}"[:200]
                    break
            if run["error"]:
                break
        run["seconds"] = round(time.monotonic() - started, 1)
        return run

    def kill_runner(self):
        """End the WDA runner app on this simulator, as a crash would."""
        subprocess.run(["xcrun", "simctl", "terminate", self.args.udid, self.args.runner_bundle],
                       capture_output=True, timeout=30)

    READ_LISTS = (("mail", ()), ("mail", ("Transactions",)), ("notes", ("All iCloud, 13",)),
                  ("notes", ("Notes, 5",)), ("cityride", ("Activity",)), ("cityride", ("Account",)),
                  ("clouddocs", ()), ("quickbite", ("Orders",)), ("teamchat", ()), ("splitpay", ()))

    def readlist(self):
        rows = []
        agent = FrontierAgent(self.driver, None, screenshots=False)
        for app, path in self.READ_LISTS:
            result = {"app": app, "path": list(path)}
            for mode in ("drag", "glide"):
                self.driver.glide_everywhere = mode == "glide"
                os.environ["MOBSTER_GLIDE_READ_LIST"] = "on" if mode == "glide" else "off"
                try:
                    self.fresh(app)
                    for label in path:
                        self.tap(label, 1.5, prefix=True)
                    snapshot = self.driver.observe_ready(timeout=15)
                    started = time.monotonic()
                    lines = agent._read_list(snapshot)
                    result[mode] = {"seconds": round(time.monotonic() - started, 2), "rows": len(lines),
                                    "lines": lines}
                except Exception as error:
                    result[mode] = {"error": f"{type(error).__name__}: {error}"[:160]}
            self.driver.glide_everywhere = True
            drag, glide = result["drag"], result["glide"]
            if "lines" in drag and "lines" in glide:
                # Clock times the apps seed relative to now move on between the two reads ("10:03 AM" then
                # "10:04 AM" in Mail): compared without them.
                a, b = ({CLOCK.sub("<time>", line) for line in side["lines"]} for side in (drag, glide))
                result["same_rows"] = a == b
                result["missing_in_glide"] = sorted(a - b)[:10]
                result["extra_in_glide"] = sorted(b - a)[:10]
                result["ratio"] = round(glide["seconds"] / drag["seconds"], 2) if drag["seconds"] else None
            rows.append(result)
            print("readlist", app, path, {k: v for k, v in result.items() if k not in ("drag", "glide")},
                  drag.get("seconds"), glide.get("seconds"), drag.get("rows"), glide.get("rows"), flush=True)
        done = [r for r in rows if "ratio" in r]
        drag_s = sum(r["drag"]["seconds"] for r in done)
        glide_s = sum(r["glide"]["seconds"] for r in done)
        self.save("readlist", {"summary": {"test": "readlist", "lists": len(done),
                                           "same_rows": sum(r["same_rows"] for r in done),
                                           "drag_s": round(drag_s, 1), "glide_s": round(glide_s, 1),
                                           "ratio": round(glide_s / drag_s, 2) if drag_s else None,
                                           "pass": bool(done) and all(r["same_rows"] for r in done)
                                           and glide_s <= .5 * drag_s}, "rows": rows})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--udid", required=True)
    parser.add_argument("--wda-url", required=True)
    parser.add_argument("--mjpeg-url")
    parser.add_argument("--xctestrun")
    parser.add_argument("--team", help="the Apple team the runner was built with; its runner id carries the "
                                       "team's suffix (signing.runner_bundle_id)")
    parser.add_argument("--runner-bundle", help="the runner app's bundle id (default: from --team)")
    parser.add_argument("--manifest", default=str(pathlib.Path.home() / "Documents/GitHub/iOSWorld/iphone/bootstrap/"
                                                                       ".app_manifest.json"))
    parser.add_argument("--rates", type=lambda text: [int(v) for v in text.split(",")], default=[60, 120, 180, 240])
    parser.add_argument("--out", required=True)
    parser.add_argument("tests", nargs="+", choices=("multiline", "paste", "typing", "cart", "apps", "wda", "readlist"))
    args = parser.parse_args(argv)
    args.runner_bundle = args.runner_bundle or runner_bundle_id(args.team) + ".xctrunner"
    bench = Bench(args)
    for test in args.tests:
        getattr(bench, test)()
    return 0


if __name__ == "__main__":
    sys.exit(main())
