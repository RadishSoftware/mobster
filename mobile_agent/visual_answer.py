"""Visual answers: one question about the picture on screen, answered by the VisionJudge.

The accessibility tree carries no image content, and every extracted answer
must be a cited on-screen literal, so "What colour are the person's eyes in
eyes8-06?" could only end in WAIT/BLOCKED (MobsterBench visual.eye_answer and
visual.eye_abstain, diag-5: no_progress after 15 steps, needs_clarification).

Trigger (all rule-based, all required; see ``visual_request`` and ``image_on_screen``):

* the answer schema is one string field, and the request asks about what a
  picture shows ("what colour", "... colour of", "eye/hair/fur colour",
  "wearing", "is there a dog in this photo", "does the image show") about a
  picture or a person/animal in it; never a text-reading question, a question
  over several pictures (a survey, loops.py) or a protected characteristic;
* the answer set is closed: the schema's enum (``extraction.closed_answers``),
  or a list the request gives after "(" or ":" ("(blue, green, grey, hazel or
  brown)"), or the fixed eye colours for an eye-colour question, or yes/no for
  a yes/no question; an open question without one never triggers;
* the screen shows exactly one dominant Image element (at least half the
  screen wide and ``IMAGE_MIN_AREA`` of it: a Files/Photos viewer, not a grid
  of thumbnails), and when the request names a file ("eyes8-06") that name is
  shown on screen (the viewer's title), so the wrong picture is never judged.

The answer is a visual judgment, not a literal: it is one of the allowed
answers, has no citations, and is recorded with the image hash and judge tier
(``visual_evidence``). An unsure judge is an honest abstention.
"""

from __future__ import annotations

import hashlib
import re

from .vision_judge import EYE_COLOUR_THRESHOLDS, EYE_COLOURS, eye_colour_steps, person_gate_steps


# The Files viewer shows a portrait photo at 1.0 x 0.32 of the screen (diag-5,
# "sunglasses12-01"); a grid thumbnail is at most 0.3 x 0.22 (and in Files a Cell).
IMAGE_MIN_WIDTH = .5
IMAGE_MIN_AREA = .12
JUDGE_TIMEOUT = 8.0
ABSTAIN = "unsure"
# Judge outcomes that say nothing about the picture: the step may be tried again.
TRANSIENT = ("model error", "timeout", "malformed model output", "unreadable image", "judge_error", "no_image")

_VISUAL = re.compile(
    r"\b(?:what|which)\s+colou?rs?\b|\bcolou?rs?\s+of\b|\b(?:eye|eyes|hair|fur|coat)\s+colou?rs?\b"
    r"|\bwear(?:s|ing)?\b"
    r"|\b(?:is|are)\s+there\s+(?:an?\s+|any\s+)?[\w\s-]{1,30}?\s+in\s+(?:this|the|that)\s+(?:photo|picture|image|pic)\b"
    r"|\b(?:photo|picture|image|pic)\s+(?:shows?|contains?|depicts?)\b"
    r"|\b(?:does|do)\s+(?:this|the|that)\s+(?:photo|picture|image|pic)\s+(?:show|contain|depict)\b", re.I)
_SUBJECT = re.compile(r"\b(?:photos?|pictures?|images?|pics?|person'?s?|people|man|woman|boy|girl|child|baby|"
                      r"eyes?|hair|dog|cat|animal|pet|bird|face)\b", re.I)
# Text is read by extraction (with OCR when AX omits it), never judged.
_TEXT = re.compile(r"\b(?:say|says|said|read|reads|text|written|word|words|number|caption|title)\b", re.I)
# The judge never decides these (vision_judge module docstring; loops refuses them too).
_PROTECTED = re.compile(r"\b(?:skin|race|racial|ethnic\w*|religio\w*|gender|sex\w*|disab\w*|health\w*|pregnan\w*|"
                        r"age|old|weight|fat|nationality)\b", re.I)
# A question over several pictures is a survey (loops.py), never one judgment.
_COLLECTION = re.compile(r"\b(?:each|every|all|which of|how many)\b", re.I)
_YES_NO = re.compile(r"^(?:is|are|does|do|has|have|can you see)\b", re.I)
_EYES = re.compile(r"\beyes?\b", re.I)
_ABSTAIN_ALLOWED = re.compile(r"\b(?:can(?:not|'t| not)\s+tell|unsure|not\s+sure|don'?t\s+know|unclear|unknown|"
                              r"can(?:not|'t| not)\s+be\s+seen|say\s+so)\b", re.I)
_ABSTAIN_OPTION = re.compile(r"(?:unsure|not sure|unclear|unknown|cannot tell|can't tell|don't know)", re.I)
_LIST = re.compile(r"\(([^()]{3,200})\)|:\s*([^.?!:()\n]{3,200})")
_OPTION = re.compile(r"[A-Za-z][A-Za-z'-]*(?: [A-Za-z][A-Za-z'-]*){0,2}")
_NAMED = re.compile(r"(?<![\w-])([A-Za-z][A-Za-z0-9]*[-_][0-9]{1,4})(?![\w-])")
_SENTENCE = re.compile(r"(?<=[.?!])\s+")


def listed_options(text):
    """The answer list a request gives in parentheses or after a colon, or None.

    "(blue, green, grey, hazel or brown)" -> [blue, green, grey, hazel, brown].
    Every entry must be a short word or phrase, so a parenthetical remark is not a list.
    """
    for match in _LIST.finditer(text or ""):
        body = match.group(1) or match.group(2)
        parts = [part.strip(" '\"‘’“”") for part in
                 re.split(r"\s*,\s*(?:or\s+)?|\s+or\s+|\s*/\s*", body.strip())]
        parts = [part for part in parts if part]
        if len(parts) >= 2 and all(_OPTION.fullmatch(part) for part in parts):
            return parts
    return None


def _question(goal):
    sentences = [s for s in _SENTENCE.split(goal or "") if s.strip()]
    asked = [s for s in sentences if "?" in s and _VISUAL.search(s)]
    return (asked or [s for s in sentences if _VISUAL.search(s)] or [None])[0]


def visual_request(goal, schema):
    """What to ask the judge, or None when the request is not a visual question (see module docstring)."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or len(properties) != 1:
        return None
    (field, spec), = properties.items()
    if not isinstance(spec, dict) or spec.get("type") != "string":
        return None
    question = _question(goal)
    if (question is None or not _SUBJECT.search(question) or _TEXT.search(question)
            or _PROTECTED.search(question) or _COLLECTION.search(question)):
        return None
    eye_colour = bool(_EYES.search(question)) and bool(re.search(r"colou?r", question, re.I))
    stated = spec.get("enum") if isinstance(spec.get("enum"), list) else listed_options(question)
    abstain_allowed = bool(_ABSTAIN_ALLOWED.search(goal))
    options = []
    for option in stated or ():
        if not isinstance(option, str) or not option.strip():
            return None
        if _ABSTAIN_OPTION.fullmatch(option.strip()):
            abstain_allowed = True  # "or 'unsure'": the request's own no-answer
        else:
            options.append(option.strip())
    if not stated:
        if eye_colour:
            options = list(EYE_COLOURS)
        elif _YES_NO.match(question.strip()):
            options = ["yes", "no"]
    # The judge's choices: short lowercase labels, the abstention last (vision_judge._validate).
    choices = {}
    for option in options:
        label = re.sub(r"\s+", " ", option.lower())
        if not re.fullmatch(r"[a-z0-9][a-z0-9 _-]{0,39}", label) or label == ABSTAIN:
            return None
        choices.setdefault(label, option)
    if not 2 <= len(choices) <= 11:
        return None
    named = _NAMED.findall(goal or "")
    return {"field": field, "question": question.strip()[:1000], "choices": choices,
            "abstain_allowed": abstain_allowed, "eye_colour": eye_colour,
            "named": named[-1] if named else None}


def image_on_screen(snapshot, request):
    """The one dominant Image element showing the requested picture, or None."""
    images = {}
    for element in snapshot.elements:
        x, y, w, h = element.rect
        if element.role == "Image" and w >= IMAGE_MIN_WIDTH and w * h >= IMAGE_MIN_AREA:
            images.setdefault(tuple(round(n, 2) for n in element.rect), element)
    if len(images) != 1:
        return None
    if request.get("named"):
        name = re.compile(r"(?<![\w-])" + re.escape(request["named"]) + r"(?![\w-])", re.I)
        if not any(name.search(element.label or "") for element in snapshot.elements):
            return None
    return next(iter(images.values()))


def _steps(request):
    """The judge's decomposition for this question, or None for a single step."""
    choices = request["choices"]
    if request["eye_colour"]:
        normal = {("grey" if label == "gray" else label): label for label in choices}
        if not set(normal) <= set(EYE_COLOURS):
            return None
        from dataclasses import replace
        steps = eye_colour_steps(EYE_COLOURS)
        # An iris colour the request does not allow abstains, never maps to a neighbour.
        mapping = {colour: normal.get(colour, ABSTAIN) for colour in EYE_COLOURS}
        mapping["unclear"] = ABSTAIN
        return (*steps[:-1], replace(steps[-1], mapping=mapping))
    if set(choices) == {"yes", "no"} and re.search(r"\bwear", request["question"], re.I):
        return person_gate_steps(request["question"], ("yes", "no", ABSTAIN))
    return None


def _threshold(request, answer):
    """The calibrated floor for an answer, on top of the judge's own threshold."""
    if request["eye_colour"]:
        return EYE_COLOUR_THRESHOLDS.get("grey" if answer == "gray" else answer, .95)
    return 0.0


def latest_image(driver, budget=3.0, still_seconds=.15):
    """A colour frame of the settled screen (same policy and .15 s as loops.LoopRunner._latest_image and
    loops.FRAME_STILL_SECONDS), else a WDA still."""
    from .frontier import video_frame
    frame = video_frame(driver, still_seconds)
    return frame if frame is not None else still_image(driver, budget)


def still_image(driver, budget=3.0):
    """A full-resolution WDA screenshot as bytes, or None."""
    from .loops import decode_image
    capture = getattr(driver, "capture_preview", None)
    if not callable(capture):
        return None
    return decode_image(capture(timeout=min(3, budget)))


def _digest(image):
    data = image if isinstance(image, (bytes, bytearray)) else repr(image).encode()
    return hashlib.sha256(bytes(data)).hexdigest()[:16]


def judge_image(judge, request, element, driver, *, crop, timeout):
    """Ask the judge about the picture; returns an outcome dict.

    ``answer`` is the request's own spelling of an allowed answer, or None to
    abstain; ``transient`` marks an outcome that says nothing about the picture
    (no frame, a model error), so the caller may try again on a later step.
    """
    image = latest_image(driver, timeout)
    evidence = {"source": "vision_judge", "element_id": element.id, "rect": [round(n, 4) for n in element.rect]}
    if image is None or crop is None:
        return {"answer": None, "transient": True, "reason": "no_image", "evidence": evidence}
    evidence["image_sha"] = _digest(image)
    try:
        crops = [crop(image, list(element.rect))]
    except Exception:
        return {"answer": None, "transient": True, "reason": "no_image", "evidence": evidence}
    hires_cache = {}

    def hires(index):
        # A full-resolution still for the judge's look-again tier, taken only if it asks.
        if "image" not in hires_cache:
            try:
                hires_cache["image"] = still_image(driver)
            except Exception:
                hires_cache["image"] = None
        return None if hires_cache["image"] is None else crop(hires_cache["image"], list(element.rect))

    options = {"steps": _steps(request), "hires": hires}
    try:
        import inspect
        accepted = set(inspect.signature(judge.judge).parameters)
    except (TypeError, ValueError):
        accepted = set()
    options = {key: value for key, value in options.items() if key in accepted and value is not None}
    choices = (*request["choices"], ABSTAIN)
    judgment = judge.judge(request["question"], crops, choices=choices, context=None, timeout=timeout, **options)
    answer, score = getattr(judgment, "answer", None), float(getattr(judgment, "score", 0) or 0)
    reason = getattr(judgment, "abstain_reason", None)
    evidence.update({"tier": getattr(judgment, "tier", None), "score": round(score, 4),
                     "judge_answer": answer, "abstain_reason": reason})
    if reason is None and answer in request["choices"] and score < _threshold(request, answer):
        reason = f"below calibrated threshold ({score:.2f})"
    if reason is None and answer not in request["choices"]:
        reason = "judge unsure"
    if reason is not None:
        return {"answer": None, "transient": any(mark in reason for mark in TRANSIENT), "reason": reason,
                "evidence": evidence}
    evidence["answer"] = request["choices"][answer]
    return {"answer": request["choices"][answer], "transient": False, "reason": None, "evidence": evidence}
