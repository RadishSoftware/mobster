"""The result of a Smart run: its status, outcome, reason and the answer put into the user's structure (``summarize``).
Nothing here changes a decision the loop makes."""

import json
import re

from . import harness_api
from .agent_hooks import STOP_CODES
from .engines import (
    ANTHROPIC_CREDIT,
    CREDIT_CODES,
    PROVIDER_ERROR,
    SMART,
    SMART_CONFIG,
    account_name,
    credit_provider,
    model_error,
    out_of_credit,
    plain_error,
    smart_model,
)
from .smart_events import item_text

STATUS = {"completed": "completed", "blocked": "blocked", "max_steps": "max_steps", "timeout": "timeout",
          "budget": "spend_cap", "error": "error", "approval_denied": "approval_denied",
          "approval_timeout": "approval_timeout", "stopped": "stopped"}


# Past-tense verbs "I" can go before: "I found 3 reminders" reads as "Found 3 reminders", like the steps.
# Anything else keeps it: "I have added…", "I don't see…" or "I see…" would read as a broken sentence or
# an instruction without it, and "I 95 is closed" or "I miss you" are the phone's own words.
DROPS_SUBJECT = frozenset({"found", "sent", "made", "set", "put", "wrote", "took", "got", "saw", "ran", "read",
                           "left", "opened", "closed", "couldn't"})
NOT_PAST = frozenset({"need", "feed", "bleed", "breed", "speed", "exceed", "proceed", "succeed", "heed", "seed",
                      "weed", "bed", "wed", "red", "shed", "embed"})


def without_first_person(answer):
    found = re.match(r"I ([A-Za-z]+(?:['’][A-Za-z]+)?)\b", answer or "")
    if not found:
        return answer
    verb = found.group(1).casefold().replace("’", "'")
    if verb not in DROPS_SUBJECT and not (verb.endswith("ed") and verb not in NOT_PAST):
        return answer
    return answer[2].upper() + answer[3:]


def tells_what_it_did(sentence, contract, proof=()):
    from . import contract_screen as KS
    if contract is None or not any(item.kind in ("COMMIT", "WRITE") for item in contract.items):
        return False
    said = KS.plain(sentence).rstrip(".!?…")
    return not any(said and said in KS.plain(entry.get("quote")) for entry in proof or ())


SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
LIST_NUMBER = re.compile(r"(?:^|\n[ \t]*|[:;][ \t]+)\d{1,2}\.$")


def sentence_end(text):
    """Where the first sentence of ``text`` ends (just past its mark), or None."""
    for mark in SENTENCE_END.finditer(text):
        if not LIST_NUMBER.search(text[:mark.end()]):
            return mark.end()
    return None


_MARKS = ((re.compile(r"^(?:#{1,6}\s+|>\s*)"), ""),
          (re.compile(r"!?\[([^\]\n]*)\]\([^)\s]*\)"), r"\1"),
          (re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1"), r"\2"),
          (re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])"), r"\1"),
          (re.compile(r"`([^`\n]+)`"), r"\1"))


def plain_marks(text):
    for pattern, replacement in _MARKS:
        text = pattern.sub(replacement, text)
    return text.strip()


def first_sentence(text, limit=300):
    text = str(text or "").strip()
    if not text:
        return None
    line, end = text.find("\n"), sentence_end(text)
    if line != -1 and (end is None or end > line):
        sentence = text[:line]
    elif end is not None and len(" ".join(text[:end].split())) >= 12:
        sentence = text[:end]
    else:
        sentence = text
    sentence = plain_marks(" ".join(sentence.split()))
    return sentence if len(sentence) <= limit else sentence[:limit - 1].rstrip() + "…"


def _answer_values(data):
    """The scalar values of a structured answer, as plain text to look for (numbers without grouping commas)."""
    values = []
    for value in (data.values() if isinstance(data, dict) else ()):
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            values.append(str(value).strip())
    return values


def _mentions(sentence, value):
    plain = " ".join(str(sentence or "").casefold().replace(",", "").split())
    return " ".join(value.casefold().replace(",", "").split()) in plain


def lead_sentence(full, data):
    values = _answer_values(data)
    if not values or not full:
        return None
    rest = str(full).strip()
    first = first_sentence(rest)
    if first and any(_mentions(first, value) for value in values):
        return None
    while rest:
        end = sentence_end(rest)
        sentence = " ".join((rest[:end] if end else rest).split())
        if sentence and any(_mentions(sentence, value) for value in values):
            return sentence if len(sentence) <= 300 else sentence[:299].rstrip() + "…"
        rest = rest[end:].strip() if end else ""
    return None


def outcome_of(status, contract, answer):
    """done | check | couldnt_finish | stopped | declined."""
    if status == "approval_denied":
        return "declined"
    if status in ("stopped", "approval_timeout"):
        return "stopped"
    if status == "completed":
        unproven = [i for i in (contract.unproven() if contract else ()) if i.kind in ("COMMIT", "WRITE", "REPORT")]
        return "check" if unproven or contract is None else "done"
    if status in ("max_steps", "timeout", "spend_cap", "blocked", "error") and answer and status != "error":
        return "check" if status != "blocked" else "couldnt_finish"
    return "couldnt_finish"


def reason_of(status, outcome, raw_reason, contract, model_failed=False, model=None):
    if outcome == "done":
        return None
    if outcome == "check":
        unproven = [i.what for i in (contract.unproven() if contract else ()) if i.kind in ("COMMIT", "WRITE")]
        if unproven:
            return "The screen never showed this finished: " + "; ".join(unproven[:3]) + "."
        if status == "spend_cap":
            return "The task reached its cost limit. This is what it found before stopping."
        if status in ("max_steps", "timeout"):
            return "The task ran out of steps. This is what it found before stopping."
        return "Mobster couldn't prove every part of this on screen. Check the answer."
    plain = plain_error(raw_reason, model_failed, model)
    if plain:
        return plain
    if status == "spend_cap":
        return "The task reached its cost limit before finishing."
    if status == "max_steps":
        actions = re.match(r"(\d+) actions used", str(raw_reason or ""))
        if actions:
            return f"The task took {actions.group(1)} actions, the most one task can take, before finishing."
        return f"The task used all {SMART_CONFIG.max_steps} steps before finishing."
    if status == "timeout":
        return "The task ran out of time before finishing."
    if status == "blocked":
        return str(raw_reason or "Mobster couldn't do this on your iPhone.")[:300]
    return str(raw_reason or "The task stopped before finishing.")[:300]


def _findings(request, answer, notes, instruction):
    """The messages of a call that puts the loop's findings (its answer and notes) into a structure."""
    return [{"role": "system", "content": instruction + " Use only values stated in its answer or notes, exactly as "
             "written there; use null where a value was not found."},
            {"role": "user", "content": [{"type": "text", "text": f"Request: {request}\n\nAnswer: {answer}\n\n"
                                          "Notes:\n" + ("\n".join(f"- {n}" for n in notes or ()) or "(none)")}]}]


def _loose_call(client, messages, schema, timeout):
    """One metered non-strict call; options ride on the call, not the shared client, so concurrent calls keep theirs."""
    options = {"timeout": timeout}
    if getattr(client, "per_call_reasoning", False) is True:
        options["reasoning"] = "none"
    if getattr(client, "per_call_strict", False) is True:
        options["strict"] = False
    out, _ = client.complete(messages, schema, **options)
    return out or {}


def call_problem(error, limit=160):
    """Why a structuring call failed, cut to ``limit``; an empty account, read on the whole error, keeps its code."""
    text = f"{type(error).__name__}: {error}"
    if out_of_credit(text):
        status = PROVIDER_ERROR.search(text)
        code = next((code for code in CREDIT_CODES + ANTHROPIC_CREDIT if code in text), None)
        return ": ".join(part for part in (type(error).__name__, status and status.group(0), code) if part)
    return text[:limit]


def structure_note(output_format, problem):
    name = AUTOMATIC_FORMATS[output_format]
    if out_of_credit(problem):
        return (
            f"Your {account_name(credit_provider(problem))} account ran out of credit before Mobster could turn this "
            f"answer into {name}, so it’s shown as text.")
    return f"Mobster couldn’t turn this answer into {name}, so it’s shown as text."


def structure(client, request, answer, notes, schema, *, timeout=60):
    """(data, None) with the answer as data matching the user's JSON schema, validated, or (None, why)."""
    from jsonschema import Draft202012Validator
    wrapped = {"type": "object", "additionalProperties": False, "required": ["data"], "properties": {"data": schema}}
    try:
        value = _loose_call(client, _findings(request, answer, notes, "Put the phone agent's findings into the "
                                              "requested structure."), wrapped, timeout).get("data")
    except Exception as error:
        return None, call_problem(error)
    if any(True for _ in Draft202012Validator(schema).iter_errors(value)):
        return None, "The answer did not fit the requested schema."
    return value, None


AUTOMATIC_FORMATS = {"csv": "CSV", "json": "JSON", "yaml": "YAML"}
# The automatic structure's one call: the findings as named columns and rows. Code turns them into records with a
# schema, the shapes Fast's automatic output takes (output_contract.validate_automatic_schema): one flat record, or
# a list of them, which is also what CSV needs.
TABLE = {"type": "object", "additionalProperties": False, "required": ["columns", "rows", "single"], "properties": {
    "columns": {"type": "array", "maxItems": 40, "items": {"type": "string"}},
    "rows": {"type": "array", "maxItems": 100,
             "items": {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}}},
    "single": {"type": "boolean"}}}
CELL = ("string", "number", "integer", "boolean", "null")


def structure_auto(client, request, answer, notes, output_format, *, timeout=60):
    instruction = (f"Put the phone agent's findings into a table for a {AUTOMATIC_FORMATS[output_format]} export. "
                   "columns: one short, readable name per field, lower case, like title or due_date. rows: one row "
                   "per item the request asks about, one value per column in the same order. single: true when the "
                   "answer is about one thing, false for a list of things.")
    try:
        out = _loose_call(client, _findings(request, answer, notes, instruction), TABLE, timeout)
    except Exception as error:
        return None, None, call_problem(error)
    return table_records(out, output_format)


def table_records(out, output_format):
    from jsonschema import Draft202012Validator

    from .output_contract import validate_automatic_schema
    if not isinstance(out, dict):
        return None, None, "The answer did not come back as a table."
    columns, rows, single = out.get("columns"), out.get("rows"), out.get("single") is True
    if not isinstance(columns, list) or not isinstance(rows, list) or not all(isinstance(row, list) for row in rows):
        return None, None, "The answer did not come back as a table."
    columns = [" ".join(str(name).split())[:64] for name in columns]
    if not columns or not all(columns) or len(set(columns)) != len(columns) \
            or any(len(row) != len(columns) for row in rows) or single and len(rows) != 1:
        return None, None, "The table's columns and rows did not line up."
    record = {"type": "object", "additionalProperties": False, "required": list(columns),
              "properties": {name: {"type": list(CELL)} for name in columns}}
    schema = record if single else {"type": "array", "maxItems": 100, "items": record}
    records = [dict(zip(columns, row)) for row in rows]
    data = records[0] if single else records
    try:
        validate_automatic_schema(output_format, schema)  # its copy sorts the keys: the columns keep their order
    except ValueError as error:
        return None, None, str(error)
    if any(True for _ in Draft202012Validator(schema).iter_errors(data)):
        return None, None, "The table's values did not fit its columns."
    return data, schema, None


def shaped_answer(text, schema):
    """The DONE turn's data_json parsed, when it validates against the user's ``schema``; else None."""
    if schema is None or not isinstance(text, str) or not text.strip():
        return None
    try:
        value = json.loads(text)
        from jsonschema import Draft202012Validator
        if any(True for _ in Draft202012Validator(schema).iter_errors(value)):
            return None
    except Exception:
        return None
    return value


def untouched_writes(contract):
    """WRITE items counted done though nothing was typed in their app: the model judged the text already there."""
    from . import contract_screen as KS
    if contract is None:
        return []

    def touched(item, typed):
        if not contract._app_ok(item, typed["app"]):
            return False
        if not typed["search"]:
            return True
        return (bool(item.payload) and KS.overlaps(item.payload, typed["text"])
                or bool(KS.SEARCHY.search(item.what)) or KS.overlaps(item.what, typed["text"]))
    return [item for item in contract.items if item.kind == "WRITE" and not item.unproven and not any(
        touched(item, t) for t in contract.typed)]


def summarize(outcome, agent, events, *, request, output_format="auto", output_schema=None, cost_usd=None,
              report=None, markdown=None):
    if report is not None:
        return _summarize_report(outcome, agent, events, request=request, output_format=output_format,
                                 output_schema=output_schema, cost_usd=cost_usd, report=report, markdown=markdown)
    contract = getattr(agent, "_contract", None)
    raw_status = outcome.get("status") or "error"
    status = STATUS.get(raw_status, "error")
    full = (outcome.get("answer") or "").strip() or None
    kind = outcome_of(status, contract, full)
    untouched = untouched_writes(contract) if kind == "done" else []
    if untouched:
        kind = "check"
    if status == "completed" and kind == "check":
        status = "completion_not_confirmed"
    model = getattr(getattr(agent, "client", None), "model", None) or smart_model()
    reason = reason_of(status, kind, outcome.get("reason"), contract,
                       bool(getattr(getattr(agent, "client", None), "model_failed", False)) and status == "error",
                       model)
    if untouched:
        reason = "Already done, so Mobster changed nothing: " + "; ".join(item_text(i) for i in untouched[:3]) + "."
    proof = events.final_proof() if events is not None else []
    # The answer is its first sentence, a headline (the dashboard shows the rest of ``data`` under it). Only a
    # sentence about what the agent did loses its "I"; words read on the phone stay as they were.
    said = without_first_person(full) if full and tells_what_it_did(first_sentence(full), contract, proof) else full
    summary = {"event": "result", "status": status, "engine": SMART, "outcome": kind,
               "answer": first_sentence(said), "reason": reason, "independently_verified": kind == "done",
               "actions": outcome.get("actions"), "decisions": outcome.get("steps"),
               "elapsed_ms": round(outcome["elapsed"] * 1000) if outcome.get("elapsed") is not None else None,
               "costUsd": cost_usd if cost_usd is not None else outcome.get("cost_usd"),
               "model": model, "contract": outcome.get("contract"),
               "timing": outcome.get("timing"), "proof": proof}
    program = outcome.get("program")
    if isinstance(program, dict) and program.get("replayed"):
        # A saved or taught program ran steps with no model: the Mac app says "Learned" (lib/replay.ts runProgram).
        summary["program"] = {key: program.get(key) for key in ("steps", "replayed", "status", "taught", "name",
                                                                  "trusted", "helped", "practice", "corrections",
                                                                  "first")}
    if outcome.get("code"):
        summary["code"] = outcome["code"]
        if outcome["code"] in STOP_CODES:
            # The field a Quick run and a refused preflight carry too: the reason is the phone's own sentence.
            summary["stop_code"] = outcome["code"]
    if status == "error":
        failed = model_error(outcome.get("reason"),
                             bool(getattr(getattr(agent, "client", None), "model_failed", False)))
        if failed:
            summary["model_error"] = failed
    automatic = output_schema is None and output_format in AUTOMATIC_FORMATS
    if full and status in ("completed", "completion_not_confirmed", "spend_cap", "max_steps", "timeout", "blocked"):
        if (output_schema is not None or automatic) and status != "blocked":
            client = getattr(agent, "client", None)
            # The DONE turn's own data_json, when it fits the schema (or is a valid table), needs no structuring call.
            given = shaped_answer(outcome.get("data_json"), TABLE if automatic else output_schema)
            data, schema, problem = (table_records(given, output_format) if automatic
                                     else (given, output_schema, None))
            if data is None:
                if client is None:
                    problem = "No model client"
                elif automatic:
                    data, schema, problem = structure_auto(client, request, full, outcome.get("notes"), output_format)
                else:
                    data, problem = structure(client, request, full, outcome.get("notes"), output_schema)
                    schema = output_schema
            source = "automatic" if automatic else "custom"
            if data is not None:
                summary.update(data=data, resolved_output_format=output_format, schema_validated=True,
                               data_status="validated", output_schema=schema, schema_source=source, full_answer=full)
                lead = lead_sentence(said, data)
                if lead:
                    summary["answer"] = lead
            elif automatic:
                # The answer stands without a structure: its text, with a line that says why it isn't the format.
                summary.update(data=full, resolved_output_format="text", data_status="observed", full_answer=full,
                               structure_error=problem, output_note=structure_note(output_format, problem))
            else:
                summary.update(data_status="schema_validation_failed", resolved_output_format=output_format,
                               full_answer=full, structure_error=problem, schema_source=source)
                if out_of_credit(problem):
                    summary["output_note"] = (
                        f"Your {account_name(credit_provider(problem))} account ran out of credit before "
                        "Mobster could fit this answer to your schema.")
        else:
            summary.update(data=full, resolved_output_format="markdown" if output_format == "markdown" else "text",
                           data_status="observed")
            if output_format == "auto":
                # Tables and check lines in the answer are result blocks (answer_shapes.from_answer); the text keeps
                # the rest, Copy keeps it all (full_answer), and the headline is the rest's first sentence.
                from .answer_shapes import from_answer
                from .result_blocks import envelope
                rest, found = from_answer(full, request)
                blocks = envelope(found) if found else None
                if blocks is not None:
                    summary.update(blocks=blocks, data=rest or None, full_answer=full)
                    if rest:
                        said_rest = without_first_person(rest) if tells_what_it_did(first_sentence(rest), contract,
                                                                                   proof) else rest
                        summary["answer"] = first_sentence(said_rest)
                    else:
                        summary["answer"] = blocks["blocks"][0].get("title") or "Here\u2019s what Mobster found."
    else:
        summary.update(resolved_output_format=None,
                       output_intent="action_only" if full is None and kind == "done" else None)
    if not full and kind == "done":
        summary["answer"] = "Done."
    tool_blocks = tool_envelope(events, status, output_schema)
    if tool_blocks is not None:
        from .result_blocks import merge
        # What tools read comes first (an app pack's DM rows, its Reels); the answer's own cards follow.
        answer_blocks = [b for b in (summary.get("blocks") or {}).get("blocks") or ()
                         if not repeats_tool_rows(b, tool_blocks["blocks"])]
        merged = merge(tool_blocks, {"blocks": answer_blocks})
        if merged is not None:
            summary["blocks"] = merged
    return summary


def _cell_words(value):
    return " ".join(str(value or "").split()).casefold().lstrip("@")


def repeats_tool_rows(block, tool_blocks):
    if not isinstance(block, dict) or block.get("type") != "table":
        return False
    columns = block.get("columns") or ()
    first = columns[0].get("key") if columns and isinstance(columns[0], dict) else None
    values = [_cell_words((row.get("cells") or {}).get(first)) for row in block.get("rows") or ()
              if isinstance(row, dict)]
    values = [v for v in values if v and not v.isdigit()]
    if not values:
        return False
    for tool in tool_blocks:
        held = set()
        for row in tool.get("rows") or ():
            held.update(_cell_words(v) for v in (row.get("cells") or {}).values())
        for item in tool.get("items") or ():
            held.update(_cell_words(item.get(k)) for k in ("title", "creator", "to"))
        if sum(1 for v in values if v in held) >= max(2, .6 * len(values)):
            return True
    return False


def tool_envelope(events, status, output_schema):
    blocks = list(getattr(events, "tool_blocks", None) or ())
    if not blocks or output_schema is not None or status in ("stopped", "approval_denied", "approval_timeout",
                                                             "error"):
        return None
    from .result_blocks import envelope
    newest = {}
    for block in blocks:
        if isinstance(block, dict):
            newest.pop(block.get("id"), None)
            newest[block.get("id")] = block
    return envelope(list(newest.values()))


def _summarize_report(outcome, agent, events, *, request, output_format, output_schema, cost_usd, report, markdown):
    """``summarize`` for a long task with a report."""
    tldr = " ".join(str(report.get("tldr") or "").split())
    full = (markdown or "").strip() or None
    partial = bool(report.get("partial"))
    # The final report replaces the loop's answer, so its earlier DONE data cannot describe this export.
    raw = {**outcome, "answer": full, "data_json": None}
    if outcome.get("status") in ("max_steps", "timeout", "budget") and not partial:
        raw["status"] = "completed"
    summary = summarize(raw, agent, events, request=request, output_format=output_format, output_schema=output_schema,
                        cost_usd=cost_usd)
    headline = first_sentence(tldr) if tldr else first_sentence(report.get("title"))
    contract = getattr(agent, "_contract", None)
    unproven = [i for i in (contract.unproven() if contract else ()) if i.kind in ("COMMIT", "WRITE")]
    summary.update(answer=headline or summary.get("answer"), report=report, markdown=markdown,
                   outcome="check" if partial or unproven else "done")
    # A report comes from a long task (longform/report.py), so its helpers are there whenever this runs.
    helpers = harness_api.hooks().long_report()
    if not summary.get("proof") and helpers is not None:
        # What the report rests on, quoted as the screens showed it (the Mac app's proof).
        summary["proof"] = helpers.proof(report, getattr(events, "apps", None))
    if summary.get("status") == "completion_not_confirmed" and not partial and not unproven:
        summary["status"] = "completed"
    if partial:
        summary["reason"] = (helpers.partial_line(report, None) if helpers else None) or summary.get("reason")
    elif summary.get("reason") and summary.get("status") == "completed" and not unproven:
        summary["reason"] = None
    if full and output_schema is None and output_format not in AUTOMATIC_FORMATS:
        summary.update(data=markdown, resolved_output_format="markdown", data_status="observed")
    summary["independently_verified"] = False
    return summary
