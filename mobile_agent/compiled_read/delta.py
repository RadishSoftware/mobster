"""Token delta: a chained step sends the model what changed on the screen, not the whole tree.

The prompt's screen section is one line per element (``e4 Cell "General" @0,20,100,7``), its alias counting down
the screen. The agent's prompt keeps no earlier screen, so a diff alone would leave the model blind. The first
full screen of a run (and any later one a diff can't describe) becomes the *reference*: it goes once in the
prompt's cached part, and each next turn sends only

- the lines that changed or are new, whole, under their own alias;
- ``eN = reference eM`` for an element that is unchanged but moved from M to N (no arithmetic for the model);
- ``e1-e5 as in the reference`` for a run of unchanged elements that kept their alias.

A new app, a screen that shares under half of its lines with the reference (a scroll moves every row's frame) or a
diff longer than ``MAX_RATIO`` of the full screen is sent whole and becomes the new reference. Actions still
resolve through the current screen's aliases, so the diff changes what the model reads, never what an alias means.
"""

import difflib
import re

MAX_RATIO = .6
MIN_SHARED = .5
ALIAS = re.compile(r"^e(\d+) ")
HEAD = "Changes since the reference screen (turn {turn}); `eN = reference eM` is the same element, once eM:"


def _split(line):
    found = ALIAS.match(line)
    return (int(found.group(1)), line[found.end():]) if found else (None, line)


def tokens(text):
    """An estimate of tokens: four characters each. The model's own tokenizer isn't called."""
    return -(-len(text) // 4)


class Delta:
    def __init__(self):
        self.reference, self.app, self.turn, self.shipped = None, None, 0, False
        self.stats = {"full": 0, "delta": 0, "tokens_full": 0, "tokens_new": 0, "tokens_cached_read": 0}

    def screen(self, app, lines, turn=0):
        """(reference lines to put in the cached part or None, the lines for this turn's screen section)."""
        full = tokens("\n".join(lines))
        body = self._diff(app, lines) if self.reference is not None and app == self.app else None
        if body is None:
            self.reference, self.app, self.turn, self.shipped = list(lines), app, turn, False
            self.stats["full"] += 1
            self.stats["tokens_full"] += full
            self.stats["tokens_new"] += full
            return None, lines
        # The reference rides in the prompt's cached part: new input on its first turn, a cache read after.
        held = tokens("\n".join(self.reference))
        self.stats["delta"] += 1
        self.stats["tokens_full"] += full
        self.stats["tokens_new"] += tokens("\n".join(body)) + (0 if self.shipped else held)
        self.stats["tokens_cached_read"] += held if self.shipped else 0
        self.shipped = True
        return list(self.reference), body

    def _diff(self, app, lines):
        old = [_split(line)[1] for line in self.reference]
        new = [_split(line)[1] for line in lines]
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        if matcher.ratio() < MIN_SHARED:
            return None
        body = []
        for tag, a1, a2, b1, b2 in matcher.get_opcodes():
            if tag == "equal":
                if a1 == b1:
                    body.append(f"e{b1 + 1}-e{b2} as in the reference" if b2 - b1 > 1
                                else f"e{b1 + 1} as in the reference")
                else:
                    body.extend(f"e{b1 + k + 1} = reference e{a1 + k + 1}" for k in range(b2 - b1))
            else:
                body.extend(lines[b1:b2])
        text = [HEAD.format(turn=self.turn), *body]
        if tokens("\n".join(text)) > MAX_RATIO * tokens("\n".join(lines)):
            return None
        return text


def for_agent(agent):
    """The agent's Delta, made on first use; None with the flag off."""
    from . import enabled
    if not enabled():
        return None
    if getattr(agent, "_compiled_delta", None) is None:
        agent._compiled_delta = Delta()
    return agent._compiled_delta
