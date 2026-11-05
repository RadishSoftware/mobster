"""Bounded outcome snapshots from the run's existing proof-gated task contract.

Plan steps describe navigation. These snapshots describe what the contract could actually
confirm, with the source run retained so a resumed task never conflates item numbers.
"""

MAX_RUNS = 12
MAX_ITEMS = 24                    # contract.MAX_ITEMS
MAX_EVIDENCE = 8


def _text(book, value, limit=300):
    text = " ".join(str(value or "").split())
    text = " ".join(str(book.mask(text) or "").split())
    return text[:limit - 1].rstrip() + "…" if len(text) > limit else text


def status(items):
    """Only an explicit receipt can make an outcome complete."""
    positive = [item for item in items if item["kind"] != "FORBID"]
    if positive and all(item["outcome"] == "completed" for item in positive):
        return "completed"
    if any(item["outcome"] in ("completed", "partial") for item in positive):
        return "partial"
    return "unverified"


def snapshot(book, contract, *, final=False):
    """Copy a contract's current facts; never retain the live mutable contract or raw screen data."""
    if contract is None:
        return None
    rows = []
    for item in list(getattr(contract, "items", ()) or ())[:MAX_ITEMS]:
        try:
            gated, reason = contract.proof(item)
        except Exception:  # proof failure cannot become completion
            gated, reason = False, "proof could not be checked"
        evidence = []
        all_receipts = item.receipts or ()
        confirmed_count = sum(bool(receipt.get("receipt")) and receipt.get("receipt") != "screen changed"
                              for receipt in all_receipts) if item.kind == "COMMIT" else 0
        for receipt in all_receipts[:MAX_EVIDENCE]:
            line = _text(book, receipt.get("receipt"))
            if line:
                evidence.append({"kind": "screen_change" if line == "screen changed" else "screen_receipt",
                                 "runId": book.run_id,
                                 "turn": receipt.get("step") if type(receipt.get("step")) is int else None,
                                 "text": line, "screen": None})
        needed = item.needed() if item.kind == "COMMIT" else None
        # An open-ended "each" count, a forbidden action, and a gate satisfied by a model
        # claim alone have no bounded positive completion receipt.
        complete = bool(item.kind == "COMMIT" and gated and needed is not None
                        and confirmed_count >= needed)
        outcome = "completed" if complete else "partial" if confirmed_count else "unverified"
        rows.append({"id": item.id, "kind": item.kind, "app": _text(book, item.app, 80) or None,
                     "what": _text(book, item.what, 200), "requested": _text(book, item.payload, 200) or None,
                     "recordedValue": _text(book, item.value, 300) if item.kind == "REPORT" and item.value else None,
                     "valueTurn": item.value_step if type(item.value_step) is int else None,
                     "gateSatisfied": bool(gated), "reason": _text(book, reason or item.reason, 200) or None,
                     "outcome": outcome, "evidence": evidence,
                     "evidenceOmitted": max(0, len(all_receipts) - len(evidence))})
    return {"runId": book.run_id, "final": bool(final), "outcome": status(rows), "items": rows}


def put(book, entry):
    """Replace this run's checkpoint snapshot; keep older source runs separate on pickup."""
    if entry is None:
        return
    with book.lock:
        book.receipts = [old for old in book.receipts if old.get("runId") != entry["runId"]]
        book.receipts.append(entry)
        if len(book.receipts) > MAX_RUNS:
            book.receipt_runs_omitted += len(book.receipts) - MAX_RUNS
            book.receipts = book.receipts[-MAX_RUNS:]
        book.revision += 1


def overall(entries, run_id):
    """Only this run's contract can determine its outcome; older runs remain provenance."""
    current = next((entry for entry in reversed(entries) if entry.get("runId") == run_id), None)
    return current.get("outcome", "unverified") if current and current.get("final") else "unverified"
