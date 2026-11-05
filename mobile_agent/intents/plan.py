"""The plan phase's question: does this goal have a deterministic path? Answers with links, never with taps.

Layers, in order: ``deeplinks.LINKS`` (hand-curated; "verified" only with a real-iPhone date), then the atlas
(discovered, never verified). A proposal is a suggestion: ``deeplinks.decide`` still governs whether it opens, and
only links that open a screen are proposed (nothing that acts, writes or needs an unknown placeholder).
"""

from .. import deeplinks


def propose(goal, atlas, installed=None):
    """Up to 5 {"layer", "app", "url", "state"} for ``goal``, best first. ``installed``: bundle ids on the phone."""
    out = []
    seen = set()
    said = str(goal or "").casefold()
    for link in deeplinks.LINKS:
        if (link.app.casefold() in said and link.risk == "opens" and not link.params
                and (installed is None or not link.bundle or link.bundle in installed)):
            out.append({"layer": "links", "app": link.app, "url": link.template, "bundle": link.bundle,
                        "state": "verified" if link.verified else "known", "bestEffort": link.best_effort})
            seen.add(link.template)
    for entry in atlas.named_in(goal):
        if installed is not None and entry.bundle not in installed:
            continue
        for scheme in entry.schemes:
            url = f"{scheme}://"
            try:
                safe = deeplinks.risk_of(url) == "opens"
            except deeplinks.LinkError:
                safe = False
            if safe and url not in seen:
                out.append({"layer": "atlas", "app": entry.name, "url": url, "bundle": entry.bundle,
                            "state": "discovered", "bestEffort": False})
    return out[:5]


class Tally:
    """Link-first outcomes for the run report: ``taken`` links that opened the screen, ``missed`` that did not."""

    def __init__(self):
        self.taken = self.missed = 0

    def record(self, landed):
        if landed:
            self.taken += 1
        else:
            self.missed += 1

    def public(self):
        return {"linkFirst": {"taken": self.taken, "missed": self.missed}}


class LinkFirst:
    """The agent's first turn, when the goal names one app and a link opens it: that turn opens the link (the
    OPEN_URL skill, so ``deeplinks.decide`` and the app guard still govern) instead of asking the model for it.
    ``turn`` answers the first call, ``settle`` is told the screen the next turn sees. Counts feed the run report."""

    def __init__(self, atlas):
        self.atlas = atlas
        self.tried = False
        self.pending = None      # the bundle the link should have opened
        self.tally = Tally()

    @classmethod
    def for_device(cls, key):
        from . import atlas
        return cls(atlas.load(key))

    def settle(self, snapshot):
        if self.pending is not None:
            self.tally.record(getattr(snapshot, "bundle_id", None) == self.pending)
            self.pending = None

    def turn(self, goal, apps):
        """The model-shaped reply for the link-first turn, or None (the model decides). Once per run."""
        if self.tried:
            return None
        self.tried = True
        found = [p for p in propose(goal, self.atlas, installed=set(apps)) if not p["bestEffort"] and p["bundle"]]
        if len({p["bundle"] for p in found}) != 1:
            return None          # no app, or two: the model chooses
        link = found[0]
        self.pending = link["bundle"]
        return {"thought": f"link first: {link['url']} ({link['state']})", "plan": None, "notes_add": [],
                "checklist_updates": [], "answer": None,
                "actions": [{"operation": "OPEN_URL", "target": None, "target_label": None, "text": link["url"],
                             "app": None}]}

    def counts(self):
        if not (self.tally.taken or self.tally.missed):
            return {}
        return {"link_first": self.tally.taken + self.tally.missed, "link_first_landed": self.tally.taken,
                "model_calls_saved": self.tally.taken + self.tally.missed}
