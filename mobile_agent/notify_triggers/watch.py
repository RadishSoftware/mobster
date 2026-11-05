"""The watcher: polls the sources, guards, matches the rules and acts."""

import collections
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from .. import secret_filter
from .rules import own_app
from .sources import SourceError

HOUR = 3600.0
DEDUPE_MAX = 4096


class Limiter:
    """Sliding one-hour windows: one per rule and one for all rules. Refuses when either is full."""

    def __init__(self, per_hour):
        self.per_hour = per_hour
        self.fired = collections.defaultdict(collections.deque)
        self.all = collections.deque()

    @staticmethod
    def _trim(window, now):
        while window and now - window[0] >= HOUR:
            window.popleft()

    def allow(self, rule, now):
        window = self.fired[rule.name]
        self._trim(window, now)
        self._trim(self.all, now)
        if len(window) >= rule.per_hour or len(self.all) >= self.per_hour:
            return False
        window.append(now)
        self.all.append(now)
        return True


def masked(notification):
    """``notification`` with secret_filter.redact over its text, and whether anything was masked."""
    title, subtitle, body = (secret_filter.redact(value) for value in
                             (notification.title, notification.subtitle, notification.body))
    changed = (title, subtitle, body) != (notification.title, notification.subtitle, notification.body)
    if not changed:
        return notification, False
    return replace(notification, title=title, subtitle=subtitle, body=body), True


class Watcher:
    def __init__(self, sources, ruleset, actor, *, listener, subscriber=None, clock=time.time):
        self.sources = list(sources)
        self.ruleset = ruleset
        self.actor = actor
        self.listener = listener
        # ``subscriber(notification)`` hears each new notification once, masked, before the rules (the workflows that
        # subscribe themselves with a trigger.notification node); it needs no rule.
        self.subscriber = subscriber
        self.clock = clock
        self.limiter = Limiter(ruleset.per_hour)
        self.seen = collections.OrderedDict()
        self.failing = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mobster-notify")

    def emit(self, event, **fields):
        try:
            self.listener(event, fields)
        except Exception:  # noqa: BLE001 -- a listener's failure never stops the watcher
            pass

    def set_rules(self, ruleset):
        """Swap the rules while watching. The hourly windows stay, so editing a trigger never resets its limit."""
        self.ruleset = ruleset
        self.limiter.per_hour = ruleset.per_hour

    def start(self):
        """Prime every source, so what is already delivered never fires."""
        for source in self.sources:
            try:
                source.prime()
            except SourceError as error:
                self.failing[source.name] = error.message
                self.emit("source_error", source=source.name, message=error.message)

    def poll(self):
        for source in self.sources:
            try:
                found = source.poll()
            except SourceError as error:
                if self.failing.get(source.name) != error.message:
                    self.failing[source.name] = error.message
                    self.emit("source_error", source=source.name, message=error.message)
                continue
            if self.failing.pop(source.name, None) is not None:
                self.emit("source_ok", source=source.name)
            for notification in found:
                self.handle(notification)

    def handle(self, notification):
        if own_app(notification.app):
            self.emit("refused", reason="loop_guard", app=notification.app, source=notification.source)
            return
        if notification.key in self.seen:
            return
        self.seen[notification.key] = True
        while len(self.seen) > DEDUPE_MAX:
            self.seen.popitem(last=False)
        shown, hidden = masked(notification)
        if self.subscriber is not None:
            try:
                self.subscriber(shown)
            except Exception as error:  # noqa: BLE001 -- a subscriber's failure never stops the watcher
                self.emit("refused", reason="subscriber_failed", app=shown.app, source=shown.source,
                          message=type(error).__name__)
        for rule in self.ruleset.rules:
            if not rule.matches(shown):
                continue
            if not self.limiter.allow(rule, self.clock()):
                self.emit("refused", reason="rate_limited", rule=rule.name, app=shown.app, source=shown.source)
                continue
            self.pool.submit(self._act, rule, shown, hidden)

    def _act(self, rule, notification, hidden):
        try:
            self.actor.act(rule, notification, hidden)
        except Exception:  # noqa: BLE001 -- one action's failure never stops the watcher
            pass

    def close(self):
        self.pool.shutdown(wait=True)
        for source in self.sources:
            try:
                source.close()
            except Exception:  # noqa: BLE001
                pass
