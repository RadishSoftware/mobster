"""Fill the atlas for a simulator: its apps' bundles through simctl, then each applinks domain's AASA over the network.

The bundle read is local and quick; the AASA fetch is the only network use, runs in the background when the runner
calls ``on_device_ready``, and is cached with the atlas under the app data folder. A failure is logged and never
stops a run.
"""

import threading
import time
import urllib.request

from . import atlas as atlas_module
from . import enabled

MAX_AGE_SECONDS = 24 * 3600
AASA_BYTES = 1_000_000
AASA_PATH = "/.well-known/apple-app-site-association"


def fetch_aasa(domain, timeout=5):
    """The AASA text of ``domain`` (https only, no redirects followed beyond urllib's default), or None."""
    if not atlas_module.DOMAIN.match(domain or ""):
        return None
    with urllib.request.urlopen(f"https://{domain}{AASA_PATH}", timeout=timeout) as reply:   # noqa: S310 (https)
        return reply.read(AASA_BYTES).decode("utf-8", "replace")


def fill_aasa(atlas, fetch, log):
    """Fetch each domain with no rules yet, once per domain. ``fetch(domain)`` raises or returns text or None."""
    seen = {}
    for entry in atlas.entries.values():
        for domain in entry.domains:
            if entry.paths.get(domain):
                continue
            if domain not in seen:
                try:
                    seen[domain] = fetch(domain)
                except Exception as error:
                    seen[domain] = None
                    log(f"intents: no AASA for {domain} ({type(error).__name__})")
            if seen[domain]:
                atlas.add_aasa(entry.bundle, domain, seen[domain])
    return atlas


def harvest(udid, run, *, fetch=None, log=print, force=False, now=time.time):
    """The simulator's atlas, saved under the app data folder. A cache younger than a day is reused unless
    ``force``. Cached AASA rules carry over a bundle re-read. ``fetch=None`` never touches the network."""
    path = atlas_module.path_for(udid)
    if not force:
        try:
            if now() - path.stat().st_mtime < MAX_AGE_SECONDS:
                return atlas_module.load(udid)
        except OSError:
            pass
    fresh = atlas_module.harvest_simulator(udid, run)
    old = atlas_module.load(udid)
    for entry in fresh.entries.values():
        before = old.entries.get(entry.bundle)
        if before is not None:
            entry.paths = {d: r for d, r in before.paths.items() if d in entry.domains}
    if fetch is not None:
        fill_aasa(fresh, fetch, log)
    atlas_module.save(udid, fresh)
    return fresh


def on_device_ready(udid, log=print, *, run=None, fetch=fetch_aasa, background=True):
    """The runner's device-ready step: refresh the atlas when the flag is on. Reads bundles now (quick) and fetches
    AASA in a daemon thread. Never raises."""
    if not enabled() or not udid:
        return None
    try:
        if run is None:
            from ..sim.simctl import run as simctl_run
            run = simctl_run
        atlas = harvest(udid, run, log=log)          # bundles only; cached for a day
        if fetch is not None and any(not e.paths.get(d) for e in atlas.entries.values() for d in e.domains):
            def finish():
                try:
                    atlas_module.save(udid, fill_aasa(atlas_module.load(udid), fetch, log))
                except Exception as error:
                    log(f"intents: AASA fetch stopped ({type(error).__name__})")
            if background:
                threading.Thread(target=finish, name="mobster-intents-aasa", daemon=True).start()
            else:
                finish()
        return atlas
    except Exception as error:
        log(f"intents: atlas not refreshed ({type(error).__name__})")
        return None
