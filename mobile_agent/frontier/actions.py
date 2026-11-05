"""One action of the model's reply, parsed once where the reply enters the loop."""

import dataclasses

from .protocol import MAX_CHUNK, normalize_reply

REPLY_FIELDS = ("target", "target_label", "text", "app")


@dataclasses.dataclass(frozen=True, slots=True)
class Action:
    op: str
    target: str | None = None
    target_label: str | None = None
    text: str | None = None
    app: str | None = None
    # Carried to an action the loop inserts: the field a repaired TYPE opened, the field a split TYPE_SUBMIT typed into.
    opened_by: str | None = None
    locator: str | None = None
    role: str | None = None

    @classmethod
    def of(cls, act):
        return cls(act["operation"], *(act.get(k) for k in REPLY_FIELDS))

    # The action as the reply named it, for a skill's call.
    def reply(self):
        return {"operation": self.op, "target": self.target, "target_label": self.target_label, "text": self.text,
                "app": self.app}


def actions_of(out):
    return [Action.of(act) for act in out.get("actions") or ()]


# A provider without a grammar can answer a bare object or an unlisted op: only an op's case and spacing are fixed.
def parse_reply(out, operations):
    if not isinstance(out, dict):
        return {"actions": []}, [], "Your reply was not a JSON object with actions. Answer with the schema's fields."
    actions = out.get("actions")
    actions = [actions] if isinstance(actions, dict) else actions if isinstance(actions, list) else []
    actions = [act for act in actions[:MAX_CHUNK + 1]]
    shaped = []
    for act in actions:
        if not isinstance(act, dict):
            break
        op = act.get("op", act.get("operation"))
        op = op.strip().upper() if isinstance(op, str) else op
        if op not in operations or any(not isinstance(act.get(k), (str, type(None))) for k in ("target", "text")):
            break
        shaped.append({**act, ("op" if "op" in act else "operation"): op})
    out = normalize_reply({**out, "actions": shaped})
    out["actions"] = [{k: act.get(k) for k in ("operation",) + REPLY_FIELDS}
                      for act in out.get("actions") or () if all(isinstance(act.get(k), (str, type(None)))
                                                                 for k in REPLY_FIELDS)]
    for key, kind in (("notes_add", list), ("checklist_updates", list)):
        if not isinstance(out.get(key), kind):
            out[key] = []
    out["checklist_updates"] = [u for u in out["checklist_updates"] if isinstance(u, dict)]
    for key in ("thought", "plan", "answer"):
        if out.get(key) is not None and not isinstance(out[key], str):
            out[key] = None
    if len(out["actions"]) == len(actions) and actions:
        return out, actions_of(out), None
    left = actions[len(out["actions"])] if len(actions) > len(out["actions"]) else None
    named = left.get("op", left.get("operation")) if isinstance(left, dict) else None
    what = (f"{str(named)[:40]!r} is not one of the listed actions" if isinstance(named, str) and named.strip()
            else "an action was not an object with op, target and text" if left is not None else "it had no actions")
    return out, actions_of(out), (f"Part of your last reply was not run: {what}. Use only the listed actions, each "
                                  "with its op, target and text.")
