"""Launch-path probe for the USB iPhone: which way into an app is fastest, and what the first read costs.

READ-ONLY apps only (Settings, Safari by default). Every call is Home, an app
activation / launch / URL open, an AX source read, or a WDA settings update
(the foreground-app hint). It never taps inside an app, never types, never
creates a WDA session, and exits if WDA has none. Do not run it while another
process drives the phone.

Per app and per path, over ``--cycles`` cold (from Home) cycles, it records:

* ``dispatch_ms``: the launch call itself;
* ``first_read_ms``: the first source read after it (the ~8 s first read seen
  in the field is this number under a system overlay);
* ``ready_ms``: launch until two consecutive reads agree on a screen of that
  app (the current observe_ready rule, without the 0.5 s quiet period so the
  number isolates the app's own readiness);
* ``hinted``: whether the foreground hint (``defaultActiveApplication``) was
  set before the first read.

Paths: ``activate`` (/wda/apps/activate), ``launch`` (/wda/apps/launch,
which does not restart a running app when ``arguments`` are absent), ``url``
(/url with the app's scheme, e.g. ``prefs:`` for Settings, ``https://`` for
Safari), each with and without the hint.

  mobile_agent/.venv/bin/python -m mobile_agent.evals.launch_probe --cycles 3 --out /tmp/launch.jsonl
"""

import argparse
import json
import statistics
import time

APPS = {"com.apple.Preferences": "prefs:root=General", "com.apple.mobilesafari": "https://example.com"}


def main():
    from ..drivers import WDA, WDA_SOURCE_PATH, resolve_wda_session
    from ..state import from_wda

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--wda-url", default="http://127.0.0.1:8100")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--app", action="append", help="Bundle ids (read-only apps only)")
    parser.add_argument("--out")
    args = parser.parse_args()
    session = resolve_wda_session(args.wda_url, create=False)
    if not session:
        raise SystemExit("WDA has no session; start one from Mobster first (this probe never creates one)")
    driver = WDA(args.wda_url, session)
    driver.configure()
    records = []

    def read(timeout=20):
        started = time.monotonic()
        xml = driver.call("GET", WDA_SOURCE_PATH, timeout=timeout)
        return from_wda(xml), (time.monotonic() - started) * 1000

    def home():
        driver.call("POST", "/wda/pressButton", {"name": "home"}, 5)
        time.sleep(1.2)

    for cycle in range(args.cycles):
        for bundle in args.app or APPS:
            for path in ("activate", "launch", "url"):
                for hinted in (False, True):
                    if path == "url" and bundle not in APPS:
                        continue
                    home()
                    driver.call("POST", "/appium/settings",
                                {"settings": {"defaultActiveApplication": bundle if hinted else "auto"}}, 5)
                    started = time.monotonic()
                    try:
                        if path == "activate":
                            driver.call("POST", "/wda/apps/activate", {"bundleId": bundle}, 20)
                        elif path == "launch":
                            driver.call("POST", "/wda/apps/launch", {"bundleId": bundle}, 20)
                        else:
                            driver.call("POST", "/url", {"url": APPS[bundle]}, 20)
                        dispatch = (time.monotonic() - started) * 1000
                        first, first_ms = read()
                        previous, ready = first, None
                        while time.monotonic() - started < 15:
                            current, _ = read()
                            if (current.content_fingerprint == previous.content_fingerprint
                                    and current.bundle_id in {"", bundle} and current.elements):
                                ready = (time.monotonic() - started) * 1000
                                break
                            previous = current
                        record = {"cycle": cycle, "bundle": bundle, "path": path, "hinted": hinted,
                                  "dispatch_ms": round(dispatch), "first_read_ms": round(first_ms),
                                  "first_read_bundle": first.bundle_id, "ready_ms": round(ready) if ready else None}
                    except Exception as error:
                        record = {"cycle": cycle, "bundle": bundle, "path": path, "hinted": hinted,
                                  "error": type(error).__name__}
                    records.append(record)
                    print(json.dumps(record), flush=True)
    driver.call("POST", "/appium/settings", {"settings": {"defaultActiveApplication": "auto"}}, 5)
    home()
    driver.close()
    if args.out:
        with open(args.out, "w") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")
    groups = {}
    for record in records:
        if "error" not in record:
            groups.setdefault((record["bundle"], record["path"], record["hinted"]), []).append(record)
    print("\nbundle path hinted: dispatch / first read / ready (p50 ms)")
    for key, rows in sorted(groups.items()):
        med = lambda name: statistics.median([r[name] for r in rows if r[name] is not None] or [0])
        print(key, round(med("dispatch_ms")), round(med("first_read_ms")), round(med("ready_ms")))


if __name__ == "__main__":
    main()
