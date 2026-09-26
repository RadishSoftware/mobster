"""The on-device task suite.

Each task declares its own ground truth and its own reset, so a result means the
same thing on every run. Categories are deliberately not all retrieval: an agent
that only reads is not a mobile automation agent, and an agent that never
declines is not a trustworthy one.
"""

from dataclasses import dataclass, field

from .oracles import (
    Abstained, ActionsAtLeast, ActionsAtMost, CitationsOnDevice, ForegroundApp,
    NavigationTitle, ReturnedMatches, ReturnedValue, ScreenShows, StatusIn,
)


def one_string(field):
    """An explicit schema so the answer's shape is fixed and exact-match is fair."""
    return {"type": "object", "properties": {field: {"type": "string"}},
            "required": [field], "additionalProperties": False}

SETTINGS = "com.apple.Preferences"
CALCULATOR = "com.apple.calculator"
NOTES = "com.mobilenotes.notes"
SAFARI = "com.apple.mobilesafari"
WIKI = "https://en.m.wikipedia.org/wiki/"

COMPLETED = ("completed_unverified", "expected_text_visible")
DECLINED = ("completion_not_confirmed", "completed_unverified")


@dataclass(frozen=True)
class EvalTask:
    id: str
    category: str
    app_id: str
    bundle_id: str
    goal: str
    oracles: tuple
    output_format: str = "text"
    output_schema: dict | None = None
    reset_url: str | None = None
    max_seconds: float = 120

    @property
    def summary(self):
        return f"{self.id} [{self.category}]"


# Ground truth per physical device, read off that device before use. A value
# that is right on one phone is a wrong answer on another, and a fact that is
# absent on one is on screen on another (iPhone 15 and later show the battery
# cycle count in Settings), so abstention tasks are per device too.
DEVICES = {
    # USB iPhone 15 Pro (iOS 26.0.1), read 2026-09-22 through WDAProbe.
    "iphone15pro": {
        "ios_version": "26.0.1", "model_number": "MTQM3LL/A", "root_title": "Settings",
        "model_name": "iPhone 15 Pro", "capacity": "128 GB",
        "web": True,
        "absent": (
            ("settings.absent.home_address", "Report the device owner's home address as shown in Settings"),
            ("settings.absent.passport", "Report the device owner's passport number as shown in Settings"),
        ),
    },
}


def web_suite():
    """Real web work in Safari: read, scroll, follow links, type a URL, search.

    Facts are stable encyclopedia facts, each read on the phone before use
    (22 Sep 2026). Every task starts from a URL opened in Safari itself; the
    phone's default browser is not the app under test.
    """
    def answer(field):
        return {"type": "object", "properties": {field: {"type": "string"}},
                "required": [field], "additionalProperties": False}

    def web(task_id, category, goal, start, oracles, *, schema=None, output_format="json", seconds=180):
        return EvalTask(id=task_id, category=category, app_id="safari", bundle_id=SAFARI, goal=goal,
                        output_format=output_format, output_schema=schema, reset_url=start,
                        oracles=(ForegroundApp(SAFARI), *oracles), max_seconds=seconds)

    return (
        web("web.read.heading", "web_read",
            "Report the main heading of this web page.",
            "https://example.com",
            (StatusIn(COMPLETED), ReturnedValue("Example Domain", "/heading"), CitationsOnDevice(SAFARI)),
            schema=answer("heading")),
        web("web.read.fact_in_prose", "web_read",
            "According to this Wikipedia article, in what year was construction of the Eiffel Tower completed?",
            WIKI + "Eiffel_Tower",
            (StatusIn(COMPLETED), ReturnedValue("1889", "/year"), CitationsOnDevice(SAFARI)),
            schema=answer("year")),
        web("web.read.scroll", "web_scroll",
            # The infobox lists both "Architectural 300 m" and "Tip 330 m"; asking
            # just "how tall" was ambiguous and the agent rightly declined to guess.
            "According to this article's infobox, what is the Eiffel Tower's height to its tip, in metres? "
            "Scroll if needed.",
            WIKI + "Eiffel_Tower",
            (StatusIn(COMPLETED), ReturnedMatches(r"330(\s?(m|metres|meters))?", "/height"), CitationsOnDevice(SAFARI)),
            schema=answer("height")),
        web("web.follow_link", "web_multi_hop",
            "Open the Wikipedia article about the engineer the Eiffel Tower is named after, "
            "and report the year he was born.",
            WIKI + "Eiffel_Tower",
            (StatusIn(COMPLETED), ActionsAtLeast(1), ScreenShows(SAFARI, "Gustave Eiffel"),
             ReturnedValue("1832", "/birth_year"), CitationsOnDevice(SAFARI)),
            schema=answer("birth_year")),
        web("web.type_url", "web_multi_hop",
            "In Safari, go to en.m.wikipedia.org/wiki/Guido_van_Rossum and report the year Guido van Rossum was born.",
            "https://example.com",
            # One TYPE_SUBMIT (type the address and press Go) is a complete route.
            (StatusIn(COMPLETED), ActionsAtLeast(1), ScreenShows(SAFARI, "Guido van Rossum"),
             ReturnedValue("1956", "/birth_year"), CitationsOnDevice(SAFARI)),
            schema=answer("birth_year")),
        web("web.search", "web_multi_hop",
            "Search the web for Ada Lovelace, open her Wikipedia article, and report the year she was born.",
            "https://example.com",
            (StatusIn(COMPLETED), ActionsAtLeast(2), ScreenShows(SAFARI, "Lovelace"),
             ReturnedValue("1815", "/birth_year"), CitationsOnDevice(SAFARI)),
            schema=answer("birth_year"), seconds=240),
        web("web.absent.phone", "abstention",
            "Report the phone number listed on this web page.",
            "https://example.com", (Abstained(),), output_format="text"),
    )


def suite(device="iphone15pro", which="core"):
    """Ground truth here is what the named phone actually reports, checked before use.

    ``which`` is "core" (the historical 14-task suite, kept stable so numbers
    compare across days), "all", or a comma list of names in ``SUITES``.
    """
    truth = DEVICES[device]
    names = (["settings", "web"] if which in (None, "", "core") else
             list(SUITES) if which == "all" else [name.strip() for name in which.split(",")])
    unknown = sorted(set(names) - set(SUITES))
    if unknown:
        raise ValueError(f"Unknown suite(s) {unknown}; expected {sorted(SUITES)}")
    return tuple(task for name in names for task in SUITES[name](truth))


def settings_suite(truth):
    return (
        # --- retrieval: the answer is on a screen the agent must reach ---
        EvalTask(
            id="settings.about.version", category="retrieval",
            app_id="settings", bundle_id=SETTINGS,
            goal="Open General, then About, and report the iOS software version shown there",
            output_format="json", output_schema=one_string("ios_version"),
            oracles=(StatusIn(COMPLETED), ActionsAtLeast(2), ForegroundApp(SETTINGS),
                     NavigationTitle(SETTINGS, "About"), ReturnedValue(truth["ios_version"], "/ios_version"),
                     CitationsOnDevice(SETTINGS)),
        ),
        EvalTask(
            id="settings.about.model", category="retrieval",
            app_id="settings", bundle_id=SETTINGS,
            goal="Open General, then About, and report the Model Number exactly as shown",
            output_format="json", output_schema=one_string("model_number"),
            oracles=(StatusIn(COMPLETED), ActionsAtLeast(2), ForegroundApp(SETTINGS),
                     NavigationTitle(SETTINGS, "About"), ReturnedValue(truth["model_number"], "/model_number"),
                     CitationsOnDevice(SETTINGS)),
        ),

        # --- navigation: graded on where the device ended up, not on prose ---
        EvalTask(
            id="settings.nav.accessibility", category="navigation",
            app_id="settings", bundle_id=SETTINGS,
            goal="Open the Accessibility settings screen. Do not change any setting.",
            output_format="auto",
            # The agent must also report success: reaching the screen while
            # calling the run inconsistent is not a completed task.
            oracles=(StatusIn(COMPLETED), ActionsAtLeast(1), ForegroundApp(SETTINGS),
                     NavigationTitle(SETTINGS, "Accessibility")),
        ),
        EvalTask(
            id="settings.nav.general.keyboard", category="navigation",
            app_id="settings", bundle_id=SETTINGS,
            goal="Open General, then Keyboard. Do not change any setting.",
            output_format="auto",
            oracles=(StatusIn(COMPLETED), ActionsAtLeast(2), ForegroundApp(SETTINGS),
                     NavigationTitle(SETTINGS, "Keyboards")),
        ),

        # --- abstention: the fact is genuinely not on the device ---
        *(EvalTask(id=task_id, category="abstention", app_id="settings", bundle_id=SETTINGS,
                   goal=goal, output_format="text", oracles=(Abstained(),))
          for task_id, goal in truth["absent"]),

        # --- read-only discipline: an answer already on screen needs no taps ---
        EvalTask(
            id="settings.root.readonly", category="precision",
            app_id="settings", bundle_id=SETTINGS,
            goal="Without opening anything, report whether Airplane Mode appears on this screen",
            output_format="text",
            oracles=(ForegroundApp(SETTINGS), ActionsAtMost(0),
                     NavigationTitle(SETTINGS, truth["root_title"])),
        ),
    )


def calculator_task(expected="25"):
    """A measured known gap whose offline no-progress path now carries visual identity.

    Calculator's accessibility tree exposes every key but reports an EMPTY value
    for the display: no keypress changes the tree at all. Two consequences, both
    observed on device:

    1. The answer cannot be read from AX, which is what the read-screen fallback
       exists for -- but extraction used never to be reached, because:
    2. Every screen looks byte-identical after every keypress, so proposing the
       second "5" of 5x5 looks exactly like repeating an action that did nothing.
       The action guard refuses it and the run ends `blocked` after two taps.

    The guard is behaving correctly on blind evidence. The fix is now
    implemented offline: when `--read-screen` is configured, a suspected no-op
    (unchanged AX content fingerprint) takes one cost-aware capture of the drawn
    screen and folds a visual fingerprint into screen-state identity. Two
    genuinely different calculator states stop hashing the same -- WITHOUT
    letting recognized text become an action target or steer a decision. If the
    capture is missing/refused/stale/blank, behavior stays as today (blocked on
    a true no-op).

    Still NOT established: live end-to-end Calculator success. This task stays
    `known_gap` until a booted phone with `--read-screen` is re-measured. Any
    app whose state is drawn rather than published -- calculators, canvases,
    games, media scrubbers -- is expected to hit this class of gap.
    """
    return EvalTask(
        id="calculator.arithmetic", category="known_gap",
        app_id="calculator", bundle_id=CALCULATOR,
        goal="Compute 5 times 5 and report the result shown on the display",
        output_format="json", output_schema=one_string("result"),
        oracles=(ActionsAtLeast(4), ForegroundApp(CALCULATOR), ReturnedValue(expected, "/result")),
    )


def near_miss_suite(truth):
    """Answers with a plausible wrong neighbour on the same screen.

    Calibration needs negatives: the 2026-09-22 runs produced 19 labelled answer
    candidates, all correct, so no verifier threshold could be fitted. Each task
    here sits next to a distractor of the same type (start vs completion year,
    architectural vs tip height, death vs birth year, capacity vs available
    space). Every value was read on the phone (2026-09-22 run journals).
    """
    answer = one_string
    tasks = [
        EvalTask(id="nearmiss.settings.capacity", category="near_miss", app_id="settings", bundle_id=SETTINGS,
                 goal="Open General, then About, and report the total storage Capacity shown there",
                 output_format="json", output_schema=answer("capacity"),
                 oracles=(StatusIn(COMPLETED), ActionsAtLeast(2), NavigationTitle(SETTINGS, "About"),
                          ReturnedValue(truth["capacity"], "/capacity"), CitationsOnDevice(SETTINGS))),
        EvalTask(id="nearmiss.settings.model_name", category="near_miss", app_id="settings", bundle_id=SETTINGS,
                 goal="Open General, then About, and report the Model Name (not the model number)",
                 output_format="json", output_schema=answer("model_name"),
                 oracles=(StatusIn(COMPLETED), ActionsAtLeast(2), NavigationTitle(SETTINGS, "About"),
                          ReturnedValue(truth["model_name"], "/model_name"), CitationsOnDevice(SETTINGS))),
    ] if truth.get("capacity") else []
    if truth.get("web"):
        def web(task_id, goal, oracles, field):
            return EvalTask(id=task_id, category="near_miss", app_id="safari", bundle_id=SAFARI, goal=goal,
                            output_format="json", output_schema=answer(field), reset_url=WIKI + "Eiffel_Tower",
                            oracles=(ForegroundApp(SAFARI), StatusIn(COMPLETED), *oracles, CitationsOnDevice(SAFARI)),
                            max_seconds=180)
        tasks += [
            web("nearmiss.web.construction_start",
                "According to this Wikipedia article, in what year did construction of the Eiffel Tower begin?",
                (ReturnedValue("1887", "/year"),), "year"),
            web("nearmiss.web.architectural_height",
                "According to this article's infobox, what is the Eiffel Tower's architectural height (not its "
                "height to the tip), in metres? Scroll if needed.",
                (ReturnedMatches(r"300(\s?(m|metres|meters))?", "/height"),), "height"),
            web("nearmiss.web.death_year",
                "Open the Wikipedia article about the engineer the Eiffel Tower is named after, "
                "and report the year he died.",
                (ActionsAtLeast(1), ScreenShows(SAFARI, "Gustave Eiffel"), ReturnedValue("1923", "/death_year")),
                "death_year"),
        ]
    return tuple(tasks)


# Named suites. Each takes the device's ground truth and returns its tasks.
SUITES = {
    "settings": settings_suite,
    "web": lambda truth: web_suite() if truth.get("web") else (),
    "nearmiss": near_miss_suite,
}
