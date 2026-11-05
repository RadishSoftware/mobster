"""What Network X-ray says about an analysis. Every string has already passed the secret filter."""

from .. import result_blocks
from . import attribution
from .analyze import headline
from .trackers import CATEGORY_LABELS, TRACKING

# What a company that does several things is shown as: the one a person would want to know first.
CATEGORY_ORDER = ("advertising", "attribution", "analytics", "engagement", "crash", "social", "platform", "cdn")
NOT_SEEN = ("Names sent through encrypted DNS appear only when the connection itself names the server. iCloud Private "
            "Relay and Encrypted Client Hello hide the server's name, so those connections are counted without one.")


def _plural(count, one, many):
    return f"{count} {one if count == 1 else many}"


def named(result):
    """The apps with something to say: a domain, or a connection with no hostname."""
    return [app for app in result["apps"] if app["domains"] or app["unnamed"]["connections"]]


def only(result, name):
    """``result`` with only the app called ``name`` (any case), or the process that sent it.
    The local total goes too."""
    wanted = name.strip().casefold()
    kept = [app for app in result["apps"]
            if app["name"].casefold() == wanted or any(process.casefold() == wanted for process in app["processes"])]
    return {**result, "apps": kept, "local": {"packets": 0, "bytes": 0}}


def lines(result):
    """One sentence per app that contacted a domain, then one for traffic nobody could be blamed for."""
    seconds = result["capture"]["seconds"]
    out = [headline(app, seconds) for app in result["apps"] if app["domain_count"]
           and app["kind"] != attribution.UNATTRIBUTED_KIND]
    out += [headline(app, seconds) for app in result["apps"]
            if app["kind"] == attribution.UNATTRIBUTED_KIND and app["connections"]]
    return out


def companies(result):
    """[{"name", "category", "categoryLabel", "apps", "domains", "bytes"}] across all apps, biggest first."""
    found = {}
    for app in result["apps"]:
        for domain in app["domains"]:
            entry = found.setdefault(domain["company"], {"name": domain["company"], "categories": set(), "apps": [],
                                                         "domains": set(), "bytes": 0})
            if domain["category"]:
                entry["categories"].add(domain["category"])
            if app["name"] not in entry["apps"]:
                entry["apps"].append(app["name"])
            entry["domains"].add(domain["host"])
            entry["bytes"] += domain["up"] + domain["down"]
    out = []
    for entry in found.values():
        category = next((name for name in CATEGORY_ORDER if name in entry["categories"]), None)
        out.append({"name": entry["name"], "category": category,
                    "categoryLabel": CATEGORY_LABELS.get(category, "Other"),
                    "apps": sorted(entry["apps"]), "domains": len(entry["domains"]), "bytes": entry["bytes"]})
    out.sort(key=lambda item: (-item["bytes"], item["name"].lower()))
    return out


def live(result):
    """The numbers the Mac app shows while a capture runs: {"apps", "companies", "totals"}."""
    apps = [{"name": app["name"], "kind": app["kind"], "process": ", ".join(app["processes"]),
             "domains": app["domain_count"], "companies": len(app["companies"]), "tracking": len(app["tracking"]),
             "bytes": app["up"] + app["down"]} for app in named(result)]
    shared = companies(result)
    unattributed = next((app for app in result["apps"] if app["kind"] == attribution.UNATTRIBUTED_KIND), None)
    hosts = {domain["host"] for app in result["apps"] for domain in app["domains"]}
    tracking = {domain["host"] for app in result["apps"] for domain in app["domains"]
                if domain["category"] in TRACKING}
    return {"apps": apps, "companies": shared,
            "totals": {"apps": len([app for app in apps if app["kind"] != attribution.UNATTRIBUTED_KIND]),
                       "domains": len(hosts), "companies": len(shared), "tracking": len(tracking),
                       "bytes": sum(app["up"] + app["down"] for app in result["apps"]),
                       "unattributed": {"bytes": unattributed["up"] + unattributed["down"] if unattributed else 0,
                                        "connections": unattributed["connections"] if unattributed else 0},
                       "packets": result["capture"]["packets"]}}


def answer(result):
    """The task's one-line answer: counts across all apps, then the traffic that couldn't be blamed."""
    seconds = max(1, round(result["capture"]["seconds"]))
    total = live(result)["totals"]
    shown = [app for app in named(result) if app["kind"] != attribution.UNATTRIBUTED_KIND and app["domain_count"]]
    if not shown and not total["unattributed"]["bytes"]:
        return f"In {seconds} s your iPhone contacted no domain Mobster could name."
    text = (f"In {seconds} s {_plural(len(shown), 'app', 'apps')} contacted "
            f"{_plural(total['domains'], 'domain', 'domains')} "
            f"at {_plural(total['companies'], 'company', 'companies')}")
    if total["tracking"]:
        text += (f". {total['tracking']} of those domains {'is' if total['tracking'] == 1 else 'are'} ads, "
                 "analytics or attribution")
    text += "."
    if total["unattributed"]["bytes"]:
        text += " Mobster couldn't tell which app sent some of the traffic, so it is listed as Unattributed."
    return text


def _kb(count):
    return round(count / 1000, 1)


def blocks(result, stamp=None, kept=False):
    """The finished task's blocks: who each app talked to, every domain, and what was and wasn't seen."""
    people = []
    for app in named(result):
        grouped = {}
        for domain in app["domains"]:
            entry = grouped.setdefault(domain["company"], {"categories": set(), "domains": 0, "up": 0, "down": 0})
            if domain["category"]:
                entry["categories"].add(domain["category"])
            entry["domains"] += 1
            entry["up"] += domain["up"]
            entry["down"] += domain["down"]
        for company, entry in grouped.items():
            what = ", ".join(CATEGORY_LABELS[name] for name in CATEGORY_ORDER if name in entry["categories"])
            people.append([app["name"], company, what or "Unknown", entry["domains"], _kb(entry["up"]),
                           _kb(entry["down"])])
    people.sort(key=lambda row: (-(row[4] + row[5]), row[0].lower(), row[1].lower()))
    hosts = [[app["name"], domain["host"], domain["company"],
              CATEGORY_LABELS.get(domain["category"], "Unknown") if domain["category"] else "Unknown",
              domain["connections"], _kb(domain["up"] + domain["down"])]
             for app in result["apps"] for domain in app["domains"]]
    hosts.sort(key=lambda row: (-row[5], row[0].lower(), row[1]))
    out = []
    if people:
        out.append(result_blocks.table(
            [{"key": "app", "label": "App"}, {"key": "company", "label": "Company", "type": "brand"},
             {"key": "does", "label": "What it does"}, {"key": "domains", "label": "Domains", "type": "number"},
             {"key": "sent", "label": "Sent (KB)", "type": "number"},
             {"key": "received", "label": "Received (KB)", "type": "number"}],
            people, title="Who your apps talked to", id="companies"))
        out.append(result_blocks.table(
            [{"key": "app", "label": "App"}, {"key": "domain", "label": "Domain"},
             {"key": "company", "label": "Company", "type": "brand"}, {"key": "category", "label": "Category"},
             {"key": "connections", "label": "Connections", "type": "number"},
             {"key": "bytes", "label": "Bytes (KB)", "type": "number"}],
            hosts, title="Every domain", id="domains"))
    notes = ["Mobster read hostnames, sizes and times from your iPhone's network traffic. It decrypted nothing and "
             "kept no page content, URL path or address."]
    for app in result["apps"]:
        if app.get("note") and (app["up"] or app["down"] or app["connections"]):
            notes.append(f"**{app['name']}:** {app['note']} ({_kb(app['up'] + app['down'])} KB in "
                         f"{_plural(app['connections'], 'connection', 'connections')}.)")
    if result["local"]["packets"]:
        notes.append(f"{result['local']['packets']:,} packets stayed on your local network. Their names and addresses "
                     "aren't shown.")
    notes.append("**What Mobster couldn't see.** " + NOT_SEEN)
    if stamp:
        notes.append(f"Saved on this Mac, in Mobster's data folder under `xray/network/{stamp}`. " + (
            "The capture file is kept there too." if kept else "The capture file was deleted after the analysis."))
    out.append(result_blocks.markdown("\n\n".join(notes), title="What was read", id="notes"))
    return out
