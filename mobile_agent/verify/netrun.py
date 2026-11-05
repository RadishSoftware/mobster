"""One run's network conditions on a simulator: the proxy, the shim that points the app at it, and the cleanup.

``start`` returns the environment to launch the app with; ``stop`` ends the proxy and deletes the throwaway
certificate authority, and is safe to call twice. Nothing else on the Mac changes."""

import json
from pathlib import Path

from . import netcerts, netshim
from .netproxy import PROFILES, Proxy, ProxyError

NOT_SEEN = ("No request from the app reached the proxy. The app may use a network stack Mobster doesn't reach "
            "(see docs/testing.mdx).")
SUMMARY_ROWS = 200


class NetError(Exception):
    """What went wrong, then what to do."""

    def __init__(self, message, fix):
        super().__init__(message)
        self.message, self.fix = message, fix


class NetRun:
    def __init__(self, profile, stubs, *, udid, run, build=netshim.ensure_built, authority=netcerts.Authority,
                 proxy=Proxy, watch=None):
        self.name, self.stubs, self.udid, self.run = profile, tuple(stubs), udid, run
        self._build, self._authority, self._proxy_class = build, authority, proxy
        self.proxy = self.ca = None
        self._rows, self._total, self._captures = [], 0, []
        self.watch = None if watch is None else tuple(watch)   # hosts wire assertions need opened (wire.py)

    def start(self):
        try:
            library = self._build()
            if self.stubs or self.watch:   # only a stub or a watched host needs https opened; a profile tunnels it
                self.ca = self._authority()
                code, output = self.run(["keychain", self.udid, "add-root-cert", str(self.ca.cert)])
                if code != 0:
                    raise NetError("Mobster couldn't make the simulator trust its stub certificate"
                                   + (f": {output.splitlines()[-1][:160]}" if output else "") + ".",
                                   "Run `mobster sim doctor`, then try again, or remove stubs from the check.")
            extra = {} if self.watch is None else {"watch": self.watch}
            self.proxy = self._proxy_class(PROFILES.get(self.name), self.stubs, self.ca, **extra)
            port = self.proxy.start()
        except netshim.ShimError as error:
            self.stop()
            raise NetError(error.message, error.fix) from None
        except (netcerts.CertError, ProxyError) as error:
            self.stop()
            raise NetError(str(error), "Check that /usr/bin/openssl runs, then try again.") from None
        except NetError:
            self.stop()
            raise
        return {"DYLD_INSERT_LIBRARIES": library, "MOBSTER_PROXY_HOST": "127.0.0.1", "MOBSTER_PROXY_PORT": str(port)}

    def stop(self):
        proxy, ca, self.proxy, self.ca = self.proxy, self.ca, None, None
        if proxy is not None:
            proxy.stop()
            self._rows, self._total = proxy.rows(), proxy.total
            self._captures = proxy.captures() if self.watch is not None else []
        if ca is not None:
            ca.close()

    def seen(self):
        """(whole requests, rows) the proxy saw so far, for wire assertions; still readable after ``stop``."""
        proxy = self.proxy
        return (proxy.captures(), proxy.rows()) if proxy is not None else (list(self._captures), list(self._rows))

    def summary(self):
        """What the proxy saw, for the result: the profile, the request count and the first rows."""
        out = {"profile": self.name, "requests": self._total, "rows": self._rows[:SUMMARY_ROWS]}
        if not out["requests"]:
            out["note"] = NOT_SEEN
        return out

    def write(self, directory):
        Path(directory, "network.json").write_text(json.dumps(self.summary(), indent=1), encoding="utf-8")

