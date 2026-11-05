"""How a run opens a link in the app under test: WebDriverAgent's /url on an iOS device, `simctl openurl` as the
simulator's fallback for its 404, and the manager's own opener where there is no WebDriverAgent (Android). Each
open is journaled before it is sent, and one whose outcome is unknown is never sent again."""

import urllib.error

from ..transport import TransportError
from .smart import is_real_device
from .verdict import CouldntRun


class RunLinks:
    def _learn_link(self, url):
        """Keep a link that opened as learned for the project (devkit/links.py). Never fails the run."""
        if self.check.source:
            from ..devkit import links
            from .checks import project_root
            links.learn(project_root(self.check.source), url)

    def _open_link(self, url):
        if getattr(self.driver, "call", None) is None:  # no WebDriverAgent (Android): the manager opens it
            with self.actions.action("OPEN_URL"):   # journaled before it is sent, as on a simulator
                self._call(self.manager.open_url, self.target, url)
            self._learn_link(url)
            return
        # `simctl openurl` stops at SpringBoard's "Open in “App”?" (iOS 26.4): it is only the fallback for WDA's 404.
        # An unknown outcome never opens the link again.
        try:
            self.driver.call("POST", "/url", {"url": url, "bundleId": self.app.bundle_id}, timeout=20)
        except Exception as error:
            unsupported = ((isinstance(error, urllib.error.HTTPError) and error.code == 404)
                           or (isinstance(error, TransportError) and str(error) == "HTTP 404; request not retried"))
            if not unsupported:
                raise CouldntRun("unknown_outcome",
                                 f"WebDriverAgent didn't confirm whether the link opened in {self.app.name} "
                                 f"({type(error).__name__}).",
                                 "Check the app before starting another run; Mobster didn't open the link again.") \
                    from None
            # The fallback opens the link system-wide, in whichever app takes it: never on someone's phone.
            if is_real_device(self.manager):
                raise CouldntRun("launch", f"WebDriverAgent couldn't open the link in {self.app.name} on the "
                                           f"iPhone ({type(error).__name__}).",
                                 "Update WebDriverAgent in the Mobster app, or run the check on a simulator with "
                                 "--sim.") from None
            self.log(f"WDA didn't open the link ({type(error).__name__}); opening it with simctl")
            with self.actions.action("OPEN_URL"):
                self._call(self.manager.open_url, self.target, url)
        self._learn_link(url)
