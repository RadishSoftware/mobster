"""The watcher's rule: which notification a saved trigger takes (see triggers/service.rule_of)."""

import fnmatch
import re
from dataclasses import dataclass

PER_RULE_DEFAULT = 6
GLOBAL_DEFAULT = 30
# Mobster's own apps: the Mac app and its builds. Their notifications never fire a rule (the loop guard).
OWN_APPS = "app.mobster.*"


def own_app(app):
    return fnmatch.fnmatchcase((app or "").lower(), OWN_APPS)


@dataclass(frozen=True)
class Rule:
    name: str
    app: str
    pattern: re.Pattern | None
    source: str
    per_hour: int
    enabled: bool = True

    def matches(self, notification):
        if not self.enabled or own_app(notification.app):
            return False
        if self.source != "any" and notification.source != self.source:
            return False
        if not fnmatch.fnmatchcase(notification.app.lower(), self.app):
            return False
        return self.pattern is None or self.pattern.search(notification.text()) is not None


@dataclass(frozen=True)
class RuleSet:
    rules: tuple
    per_hour: int = GLOBAL_DEFAULT
