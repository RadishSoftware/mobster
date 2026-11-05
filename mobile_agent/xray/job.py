"""One App X-ray run: no model, only taps the policy allows; screen text is masked and never an instruction."""

import json
import os
import re
import time
from pathlib import Path

from .. import harness_api
from .. import result_blocks as blocks
from ..paths import user_data_dir
from . import policy, report
from .crawl import Caps, Crawler

GOAL = "Map {name} with App X-ray"
MAX_GALLERY = 60
DEFAULT = {"screens": 30, "minutes": 5}
LIMITS = {"screens": (1, 120), "minutes": (1, 30)}


def clean(value):
    """The run field ``xray``: ``{"screens": 1..120 (30), "minutes": 1..30 (5)}``. ValueError with a sentence."""
    if value is True or value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(LIMITS):
        raise ValueError("xray is an object with screens (1 to 120) and minutes (1 to 30)")
    out = dict(DEFAULT)
    for name, (low, high) in LIMITS.items():
        if name in value:
            number = value[name]
            if type(number) is not int or not low <= number <= high:
                raise ValueError(f"xray.{name} is a whole number from {low} to {high}")
            out[name] = number
    return out


def find_app(runtime, wanted, device=None):
    phone = runtime.resolve_device(device) if getattr(runtime, "fleet", None) is not None else None
    apps = [a for a in runtime.apps_for(phone) if a.get("bundleId") and a.get("installed") is not False]
    wanted = wanted.strip()
    return (next((a for a in apps if a["bundleId"] == wanted), None)
            or next((a for a in apps if a.get("name", "").casefold() == wanted.casefold()), None))


def folder_for(bundle, now=None):
    """A new 0700 folder for this crawl under the data folder (the newest never replaces an older one)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", bundle)[:128] or "app"
    root = user_data_dir() / "xray"
    folder = root / "apps" / safe / time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    n = 1
    while folder.exists():
        n += 1
        folder = folder.with_name(f"{folder.name}-{n}")
    folder.mkdir(parents=True, mode=0o700)
    for path in (folder, root):
        os.chmod(path, 0o700)
    return folder


def shown_folder(folder):
    """The folder as the result shows it: under the data folder, so a shared result names no account on this Mac."""
    try:
        return str(Path(folder).relative_to(user_data_dir()))
    except ValueError:
        return Path(folder).name


def app_name(ctx, result):
    try:
        for app in ctx.runtime.apps():
            if app.get("bundleId") == ctx.app_bundle and app.get("name"):
                return app["name"]
    except Exception:  # noqa: BLE001 -- the name is a nicety
        pass
    return report.app_name(result)


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def answer_line(name, result):
    stats = result["stats"]
    line = (f"Mapped {plural(stats['screens'], 'screen')} of {name}, {plural(stats['max_depth'], 'tap')} deep, in "
            f"{report.duration(stats['wall_seconds'])}, with no model.")
    if stats["paywalls"]:
        line += f" {plural(stats['paywalls'], 'paywall')}."
    if stats["refused"]:
        line += f" It left {plural(stats['refused'], 'control')} alone because they could act."
    return line


def result_blocks(result, folder):
    """The gallery of screens, the paywall plans, and the stats, as validated result blocks."""
    stats, out = result["stats"], []
    items = [{"id": s["id"], "frameId": s["frame_id"], "caption": s["name"]}
             for s in result["screens"] if s.get("frame_id")][:MAX_GALLERY]
    if items:
        out.append(blocks.gallery(items, title="Screens"))
    rows = [(s["name"], plan) for s in result["screens"] if s.get("paywall") for plan in s["paywall"]["plans"]]
    if rows:
        currencies = {plan["currency"] for _, plan in rows}
        money = len(currencies) == 1       # one currency: a money column; else the price as shown
        price = ({"key": "price", "label": "Price", "type": "money", "currency": next(iter(currencies))} if money
                 else {"key": "price", "label": "Price"})
        out.append(blocks.table(
            [{"key": "screen", "label": "Screen"}, {"key": "plan", "label": "Plan"}, price,
             {"key": "period", "label": "Period"}, {"key": "trial", "label": "Trial"}],
            [{"screen": name, "plan": plan["name"] or plan["period"] or plan["line"],
              "price": plan["amount"] if money else plan["price"],
              "period": report.PERIODS.get(plan["period"], plan["period"]), "trial": plan["trial"] or "None"}
             for name, plan in rows], title="Paywall plans", id="plans"))
    lines = [f"- Screens: {stats['screens']}, links: {stats['edges']}, taps: {stats['taps']}, scrolls: "
             f"{stats['scrolls']}", f"- Time: {report.duration(stats['wall_seconds'])}",
             f"- It stopped because it {result['stopped']}.",
             f"- Left alone: {stats['refused']} controls that could act",
             "- Model cost: $0", f"- Saved on this Mac in Mobster's data folder: {shown_folder(folder)}"]
    out.append(blocks.markdown("\n".join(lines), title="How it went", id="stats"))
    return blocks.envelope(out)


def failure(reason, **fields):
    return {"event": "result", "status": "error", "engine": "smart", "outcome": "couldnt_finish", "answer": None,
            "reason": reason, "independently_verified": False, "proof": [], "actions": 0, "decisions": 0,
            "costUsd": 0.0, "model": None, **fields}


def run(ctx, driver):
    """The result of the crawl for ``ctx`` (a run with the ``xray`` field): done, stopped, or why it could not map."""
    options = ctx.extras["xray"]
    bundle = ctx.app_bundle
    refusal = policy.app_refusal(bundle) if bundle else None
    if refusal or not bundle:
        return failure(refusal or "App X-ray maps one app. Pick an app, then run it again.")
    started = time.monotonic()
    folder = folder_for(bundle)
    log_file = folder / "crawl.log"

    def log(message):
        try:
            with open(log_file, "a", encoding="utf-8") as handle:
                handle.write(f"{time.monotonic() - started:7.1f}  {message}\n")
        except OSError:
            pass

    crawler = Crawler(driver, bundle, caps=Caps(screens=options["screens"], seconds=options["minutes"] * 60.0),
                      out_dir=folder, emit=ctx.emit, cancelled=ctx.cancelled, log=log)
    broke = None
    try:
        result = crawler.run()
    except Exception as error:  # noqa: BLE001 -- a lost phone or a driver fault: keep the map so far, retry nothing
        broke = error
        crawler.stopped = f"lost the phone ({type(error).__name__}); the last tap was not retried"
        result = crawler.map()
    (folder / "map.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    report.write(result, folder)
    if not result["screens"]:
        return failure(f"Mobster mapped no screens: it {result['stopped']}. Nothing was tapped.",
                       xray={"folder": str(folder)})
    stopped = ctx.cancelled()
    from ..memory import writer
    writer.from_xray(ctx.run_id, bundle, result)  # saved as facts when the run finishes
    name = app_name(ctx, result)
    # Every screen in the map is one of Mobster's own screenshots, so the result is proven, not a guess.
    proof = [{"quote": f"Every screen in this map is a screenshot Mobster took in {name} on your iPhone.", "app": name}]
    done = {"event": "result", "engine": "smart", "answer": answer_line(name, result),
            "independently_verified": True, "proof": proof, "actions": result["stats"]["taps"], "decisions": 0,
            "elapsed_ms": round((time.monotonic() - started) * 1000), "costUsd": 0.0, "model": None,
            "blocks": result_blocks(result, folder),
            "xray": {"folder": str(folder), "map": str(folder / "map.json"), "report": str(folder / "report.html")}}
    if stopped:
        return {**done, "status": "stopped", "outcome": "stopped", "reason": "Stopped by you. This is the map so far."}
    if broke is not None:
        return {**done, "status": "completion_not_confirmed", "outcome": "check", "actions_may_have_run": True,
                "reason": f"Mobster lost the phone and stopped: {type(broke).__name__}. This is the map so far; "
                          "the last tap was not repeated."}
    return {**done, "status": "completed", "outcome": "done", "reason": None}


def hook(ctx, driver, approve):
    """Pre-run hook: a run with ``xray`` maps the app instead of calling a model."""
    if (ctx.extras or {}).get("xray") is None or driver is None:
        return None
    return harness_api.PreRun(result=run(ctx, driver))
