"""How iOS's own moves work on each phone, kept in the learned store: which route opened an app (Spotlight, the App
Library, the Home Screen's pages), and whether Settings' search finds pages or the path has to be walked.

``ios.system`` asks a ``RouteMemory`` before it opens an app or a Settings page and tells it what worked, so the next
time goes straight to the route that worked on that phone (an app hidden from Spotlight goes straight to the App
Library). A route that stops working is forgotten at its first miss.

Each fact is one replay_core path (``kind`` "route", one live step naming the route) in the data folder's
``replays/routes/`` (0700, files 0600), keyed by the phone and the app: the same store as saved programs and learned
workflows, no second memory. No screen text is kept.
"""

from pathlib import Path

from ..replay_core.path import new_path
from ..replay_core.store import Cache, env_of

ROUTE_OP = "SYSTEM_ROUTE"
KNOWN = frozenset({"bundle", "spotlight", "app_library", "home_pages", "search", "path"})


def routes_dir(data_dir):
    return Path(data_dir) / "replays" / "routes" if data_dir else None


class RouteMemory:
    """``get(phone, what)`` -> the route that last worked, or None; ``put`` and ``miss`` record what happened."""

    def __init__(self, directory):
        self.cache = Cache(directory)

    @staticmethod
    def _keys(phone, what):
        identity = {"kind": "route", "device": phone or None, "what": str(what)[:255]}
        return identity, env_of(None, None, phone)

    def get(self, phone, what):
        identity, env = self._keys(phone, what)
        path = self.cache.load(identity, env)
        if not path or path.get("kind") != "route":
            return None
        route = (path["steps"][0] or {}).get("param")
        return route if route in KNOWN else None

    def put(self, phone, what, route):
        if route not in KNOWN or self.get(phone, what) == route:
            return
        identity, env = self._keys(phone, what)
        self.cache.save(new_path([{"op": ROUTE_OP, "live": True, "param": route}], kind="route", identity=identity,
                                 env=env))

    def miss(self, phone, what, route):
        if self.get(phone, what) == route:
            identity, env = self._keys(phone, what)
            self.cache.forget(identity, env)


def for_data_dir(data_dir):
    """The data folder's RouteMemory, or None without one (tests, a bare driver)."""
    directory = routes_dir(data_dir)
    return RouteMemory(directory) if directory is not None else None
