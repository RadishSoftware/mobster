"""Audit each XML source read a verification driver actually makes, without another WDA request."""

import threading
from urllib.parse import parse_qsl, urlencode, urlsplit

from .tree import parse_tree


def _xml_source(path):
    if not isinstance(path, str):
        return None
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or parts.path != "/source":
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    if [value for key, value in query if key == "format"] != ["xml"]:
        return None
    # Keep the caller's other source options; only attributes omitted for fast grounding are restored.
    query = [(key, value) for key, value in query if key != "excluded_attributes"]
    query.append(("excluded_attributes", "accessible,index"))
    return parts.path + "?" + urlencode(query, safe=",")


class SourceObservation:
    """A private, run-owned adapter around a driver's bound call/observe methods."""

    def __init__(self, run):
        self.run = run
        self.driver = None
        self._originals = {}
        self._wrappers = {}
        self._local = threading.local()
        self._active = False

    @property
    def active(self):
        return self._active

    def attach(self, driver):
        if self.driver is driver and self._active:
            return True
        if getattr(driver, "_verify_source_owner", None) is not None or not callable(getattr(driver, "call", None)):
            self.run._incomplete_screen("The driver could not expose its accessibility source reads")
            return False
        try:
            owned = vars(driver)
        except TypeError:
            # Restoring inherited methods requires knowing which attributes the instance owned before wrapping.
            self.run._incomplete_screen("The driver could not expose its accessibility source reads")
            return False
        originals = {"call": ("call" in owned, driver.call)}
        observe = getattr(driver, "observe", None)
        if callable(observe):
            originals["observe"] = ("observe" in owned, observe)

        def call(method, path, *args, **kwargs):
            source = _xml_source(path) if isinstance(method, str) and method.upper() == "GET" else None
            if source is None:
                return originals["call"][1](method, path, *args, **kwargs)
            release = self.run._reserve_source_read()
            try:
                xml = originals["call"][1](method, source, *args, **kwargs)
                if release is not None and self._active:
                    try:
                        tree = parse_tree(xml)
                        self.run._observe_screen(None, tree)
                        self._local.sources = getattr(self._local, "sources", 0) + 1
                    except Exception as error:
                        self.run.log(f"source audit skipped: {type(error).__name__}")
                        self.run._incomplete_screen("An accessibility source could not be audited")
                return xml
            except BaseException:
                if release is not None and self._active:
                    self.run._incomplete_screen("An accessibility source request failed")
                raise
            finally:
                if release is not None:
                    release()

        wrappers = {"call": call}
        if "observe" in originals:
            def observed(*args, **kwargs):
                before = getattr(self._local, "sources", 0)
                release = self.run._reserve_source_read()
                try:
                    snapshot = originals["observe"][1](*args, **kwargs)
                    if release is not None and self._active:
                        from ..state import PIXEL_SOURCES
                        if (getattr(snapshot, "source", None) in PIXEL_SOURCES
                                or getattr(self._local, "sources", 0) == before):
                            self.run._incomplete_screen("A screen read had no complete accessibility source")
                    return snapshot
                except BaseException:
                    if release is not None and self._active:
                        self.run._incomplete_screen("A screen observation failed")
                    raise
                finally:
                    if release is not None:
                        release()

            wrappers["observe"] = observed
        try:
            for name, wrapper in wrappers.items():
                setattr(driver, name, wrapper)
            driver._verify_source_owner = self
        except (AttributeError, TypeError):
            for name, (own, original) in originals.items():
                if getattr(driver, name, None) is wrappers.get(name):
                    if own:
                        setattr(driver, name, original)
                    else:
                        delattr(driver, name)
            self.run._incomplete_screen("The driver could not expose its accessibility source reads")
            return False
        self.driver, self._originals, self._wrappers, self._active = driver, originals, wrappers, True
        return True

    def detach(self, driver):
        if self.driver is not driver:
            return
        self._active = False
        if getattr(driver, "_verify_source_owner", None) is self:
            for name, (own, original) in self._originals.items():
                if getattr(driver, name, None) is self._wrappers[name]:
                    if own:
                        setattr(driver, name, original)
                    else:
                        delattr(driver, name)
            delattr(driver, "_verify_source_owner")
        self.driver = None
