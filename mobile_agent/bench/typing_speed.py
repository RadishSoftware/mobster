"""Typed vs pasted text on the real phone, in Settings' search field (reversible: the search is cleared after).

  python -m mobile_agent.bench.typing_speed --wda-url http://127.0.0.1:8100 --n 3

For each text length, ``WDA.write_text`` is timed with the paste threshold above the length (keystrokes) and at 1
(pasted through the edit menu), and the read-back is checked. Nothing is submitted.
"""

import argparse
import json
import statistics
import time

TEXTS = ("General", "Sounds and Haptics", "en.m.wikipedia.org/wiki/IOS_26", "Statue of Liberty New York Harbor 1886")


def field(driver):
    screen = driver.observe(timeout=10)
    found = [e for e in screen.elements if e.editable and e.role == "SearchField"]
    return (found[0] if found else None), screen


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.typing_speed")
    parser.add_argument("--wda-url", default="http://127.0.0.1:8100")
    parser.add_argument("--n", type=int, default=3)
    args = parser.parse_args(argv)
    from ..compose import build_target_driver
    from ..evals.oracles import WDAProbe
    from ..journal import Lease
    probe = WDAProbe(args.wda_url)
    lease = Lease.device(args.wda_url)
    rows = []
    try:
        driver = build_target_driver(wda_url=args.wda_url, session=probe._current_session())
        for text in TEXTS:
            for method, threshold in (("type", 10_000), ("paste", 1)):
                for _ in range(args.n):
                    probe.reset("com.apple.Preferences")
                    target, screen = field(driver)
                    if target is None:
                        raise SystemExit("no Settings search field on the root screen")
                    driver.paste_min_chars = threshold
                    started = time.monotonic()
                    receipt = driver.write_text(target, screen, text, append=False, submit=False, timeout=60)
                    seconds = time.monotonic() - started
                    rows.append({"chars": len(text), "method": method, "used": receipt.get("method"),
                                 "s": round(seconds, 2), "matches": receipt.get("matches"),
                                 "repaired": receipt.get("repaired")})
                    print(json.dumps(rows[-1]), flush=True)
        for text in TEXTS:
            for method in ("type", "paste"):
                got = [r for r in rows if r["chars"] == len(text) and r["method"] == method]
                print(f"{len(text):3} chars {method:5} median {statistics.median(r['s'] for r in got):5.2f} s "
                      f"matches {sum(bool(r['matches']) for r in got)}/{len(got)} used {[r['used'] for r in got]}")
    finally:
        probe.reset("com.apple.Preferences")
        lease.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
