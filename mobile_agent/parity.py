"""The parity matrix: every capability against the three ways in, the command line, the local API (Mobster for Mac)
and MCP (coding agents), and what is missing where.

The surfaces are read from the code: the argparse parsers, the registered and core API routes, and the MCP tool list.
``ROWS`` says which of them make one capability. ``analyse`` checks the two against each other:

- every command, subcommand and `mobster test` mode, every MCP tool and every API route group is in a row, so a
  feature that ships in one surface alone fails a test until someone says where its other surfaces are;
- everything a row names exists in the code;
- a surface a row lacks is recorded: ``no`` says why Mobster doesn't offer it there on purpose, ``gap`` ranks an open
  gap (1 is the testing ICP's, 3 is nice to have);
- a Testing row has no open MCP gap: its MCP tool exists, or the reason it doesn't is written down.

scripts/gen_docs_reference.py renders the result as docs/reference/parity.mdx; mobile_agent/tests/test_parity.py holds
the rules above.
"""

from dataclasses import dataclass, field
from fnmatch import fnmatchcase

SURFACES = ("cli", "api", "mcp")
GROUPS = ("Testing", "Release", "Phone", "Tasks and agents", "Apps", "Setup and account")


@dataclass(frozen=True)
class Row:
    name: str
    group: str
    cli: tuple = ()        # "test", "sim list", "test import maestro": the parser's command path (and `test` modes)
    api: tuple = ()        # an /api path prefix without "/api/": "tests", "tests/testflight"
    mcp: tuple = ()        # tool names; "instagram_*" is every tool that starts so
    no: dict = field(default_factory=dict)    # surface -> why Mobster doesn't offer it there on purpose
    gap: dict = field(default_factory=dict)   # surface -> (rank 1..3, what is missing)
    note: str = ""


def row(name, group, cli=(), api=(), mcp=(), no=None, gap=None, note=""):
    return Row(name, group, tuple(cli), tuple(api), tuple(mcp), dict(no or {}), dict(gap or {}), note)


T, REL, PH, TA, AP, SU = GROUPS

ROWS = (
    # -- Testing ---------------------------------------------------------------------------------------------------
    row("Run saved checks, with --locale, --a11y, --sim, --parallel", T, ["test"], ["tests"], ["run_tests"],
        note="The API serves results (recent suites, a task as a check); the Mac app starts a check as a task."),
    row("Inspect or export a completed local suite", T, ["test capsule"], [], [],
        no={"api": "Raw evidence export is an explicit local terminal selection.",
            "mcp": "Agents compare saved suites with compare_test_suites; raw evidence export stays with the person."}),
    row("Compare two exact saved suites", T, ["test compare"], [], ["compare_test_suites"],
        no={"api": "The local API shows suite results; paired comparison reads completion-bound local reports."}),
    row("Run an ordered simulator scenario", T, ["test scenario"], [], [],
        no={"api": "Scenario manifests and actor leases are local test-project operations.",
            "mcp": "The person starts the bounded multi-simulator scenario in the terminal."}),
    row("Reduce a controlled local reproducer", T, ["test minimize"], [], [],
        no={"api": "The reducer runs bounded local test suites against two explicit app builds.",
            "mcp": "The reducer has no durable MCP job and status interface; run it explicitly in the terminal."}),
    row("Run one check once", T, ["verify"], [], ["verify_start", "verify_finish"],
        no={"api": "The app starts checks as tasks (/api/runs) and shows results from /api/tests."}),
    row("Record a check from a task", T, ["test record"], ["tests"], ["save_check"]),
    row("Import Maestro flows as checks", T, ["test import maestro"], [], ["import_maestro"],
        gap={"api": (2, "The app has no import screen yet.")}),
    row("Reproduce a crash report as a check", T, ["test repro"], [], ["repro_crash"],
        gap={"api": (3, "The app has no crash intake screen yet.")}),
    row("Add a check from a recipe", T, ["test add"], [], [],
        no={"api": "The app writes checks from its editor.",
            "mcp": "An agent writes the check file itself, or saves a task as one with save_check."}),
    row("Test what a pull request changed", T, ["test pr"], [], ["run_tests"],
        no={"api": "It runs from the terminal, CI or an agent, where the diff is."}),
    row("Review and accept healed steps", T, ["test heal"], ["tests/heals"], ["review_heals"]),
    row("Read each check's history", T, ["test --history"], ["tests/history", "projects"], ["test_history"],
        note="GET /api/projects/{id}/tests includes each check’s last 20 results."),
    row("Overnight run: list it, run it now", T, ["test nightly"], [], ["test_schedule"],
        gap={"api": (3, "The app's schedules are for workflows; a nightly suite isn't listed there.")}),
    row("Scheduled health checks", T, ["test schedule"], ["tests/schedules"], [],
        no={"mcp": "The scheduler can bind real iPhones; agents run simulator checks with run_tests instead."}),
    row("Project fixtures", T, ["fixtures list", "fixtures apply"], ["tests/fixtures"], [],
        no={"mcp": "An agent writes fixture files and runs checks that use them with run_tests."}),
    row("Project deep links", T, ["links list", "links check"], ["tests/links", "projects"], [],
        no={"mcp": "An agent reads .mobster/links.yaml and tests links with run_tests or verify_start."}),
    row("Manage simulators: list", T, ["sim list", "devices"], ["devices"], ["list_devices"]),
    row("Manage simulators: prepare and clean up", T,
        ["sim prepare", "sim shutdown", "sim erase", "sim delete", "sim prune", "sim release", "sim doctor",
         "sim biometric"], [], [],
        no={"api": "Simulators belong to checks: the app never asks for one by hand.",
            "mcp": "verify_start and run_tests prepare and release simulators themselves; deleting stays with the "
                   "person."}),
    row("Run checks on every pull request (CI)", T, ["ci init", "ci run", "ci comment", "ci doctor"], [], [],
        no={"api": "It runs in CI, on a hosted runner or a Mac of yours, with no app.",
            "mcp": "CI starts it; an agent tests a branch with run_tests pr."}),
    row("Run checks after every Xcode build", T, ["xcode after-build", "xcode hook", "xcode unhook"], [], [],
        no={"api": "It is a scheme post-action.", "mcp": "Xcode starts it; an agent runs run_tests itself."}),
    row("Expo: preview and reload", T, ["expo preview", "expo reload"], [], ["expo_preview", "expo_reload"],
        no={"api": "The Mac app's tools reach Expo through MCP."}),
    row("Expo: build, dev server, doctor", T, ["expo build", "expo dev", "expo stop", "expo doctor"], [], [],
        gap={"api": (3, "No screen starts a dev server.")},
        no={"mcp": "Building and starting Metro are the developer's build step: an agent runs the same commands in "
                   "its own shell, and expo_preview opens the result."}),
    row("Test credentials for a project (env)", T, ["env list"], ["project"], [],
        no={"mcp": "The names are in .mobster/env.example, which an agent reads as a file; the values stay in the "
                   "Keychain."}),
    row("Set and remove a project's test credentials", T, ["env set", "env unset"], ["project"], [],
        no={"mcp": "Secrets are typed by the person: an agent never sets or clears one."}),
    row("Map an app's screens (X-ray)", PH, ["xray app", "xray diff"], ["xray"], [],
        gap={"mcp": (3, "An agent can't start an X-ray or read the map.")}),
    row("Record an app's network (X-ray)", PH, ["xray network"], ["xray"], [],
        no={"mcp": "It records everything the phone sends, so the person starts it."}),
    # -- Release ---------------------------------------------------------------------------------------------------
    row("TestFlight: list builds", REL, ["testflight list", "asc builds"], ["tests/testflight"], [],
        gap={"mcp": (1, "An agent can't see which build is installable.")}),
    row("TestFlight: install a build on an iPhone", REL, ["testflight install"], ["tests/testflight"], [],
        gap={"mcp": (2, "Needs the same --allow-device rule run_tests pr uses before an agent may install.")}),
    row("App Store Connect key", REL, ["testflight key"], ["tests/testflight"], [],
        no={"mcp": "A key is a credential: an agent never reads or sets it."}),
    row("App Store Connect: apps, feedback, key", REL, ["asc apps", "asc feedback", "asc key"], [], [],
        gap={"api": (3, "No screen reads tester feedback."),
             "mcp": (2, "An agent can't read TestFlight feedback or crashes.")}),
    row("App Store screenshots from checks", REL, ["asc screenshots"], [], [],
        gap={"api": (3, "No screen makes store screenshots."), "mcp": (3, "An agent can't make store screenshots.")}),
    # -- Phone -----------------------------------------------------------------------------------------------------
    row("See the screen", PH, ["screen"], ["device", "devices"], ["screen", "read_screen"]),
    row("Tap, swipe, hold and drag", PH, [], [], ["tap", "swipe", "hold_point", "flick_point", "drag_point"],
        no={"cli": "The CLI hands a task to the agent (mobster run); single taps are for coding agents.",
            "api": "The app drives the phone through tasks."}),
    row("Type text and sign-in codes", PH, ["phone password"], [], ["type_text", "use_code"],
        no={"api": "The app types through tasks."},
        note="mobster phone password types a password privately; other text goes through tasks."),
    row("Wait for the screen", PH, [], [], ["wait", "wait_for"],
        no={"cli": "Tasks wait for themselves.", "api": "Tasks wait for themselves."}),
    row("System alerts, Home, buttons", PH, ["phone button"], [], ["alert", "home", "press_button"],
        no={"api": "The app acts through tasks."}),
    row("Open a link or an app", PH, ["phone open"], [], ["open_url", "launch_app"],
        no={"api": "The app opens things through tasks."}),
    row("Install a local build", PH, ["phone install"], ["phone"], ["install_app"]),
    row("Clipboard", PH, ["phone clipboard"], ["phone"], ["set_clipboard"],
        note="Reading the clipboard is for the person only, because it may hold a password."),
    row("Files in an app's folder", PH, ["phone file"], ["phone"], ["put_file", "get_file", "list_files"]),
    row("Photos both ways", PH, ["phone photos"], ["phone"], ["send_media", "pull_media"]),
    row("Record the phone's network", PH, ["phone network"], ["phone"], [],
        no={"mcp": "It records everything the phone sends, so the person starts it."}),
    row("Record the phone's screen", PH, ["phone record"], ["phone"], [],
        gap={"mcp": (3, "An agent can't record a clip of the phone.")}),
    row("Display settings", PH, [], [], ["set_display"],
        no={"cli": "A task sets it: mobster run \"Turn on Dark Mode\".", "api": "The app sets it through a task."}),
    row("Notifications and what the phone plays", PH, [], [], ["read_notifications", "listen"],
        gap={"cli": (3, "mobster run reads notifications only inside a task."),
             "api": (3, "No screen reads Notification Center directly.")}),
    row("Lock state", PH, ["devices lock-probe"], ["status"], ["unlock_status", "unlock"],
        note="Mobster never unlocks a phone or enters a passcode: unlock only tells the agent to ask the person."),
    row("Device status", PH, ["doctor", "devices status"], ["status", "access"], ["status", "device_status"]),
    row("Use an iPhone over Wi-Fi", PH, ["wifi on", "wifi off", "wifi probe", "wifi status"], ["wireless"], [],
        no={"mcp": "Pairing a phone is the person's decision."}),
    row("Set up an iPhone", PH, ["setup"], ["setup"], [],
        no={"mcp": "Setup needs the person at the phone and the Mac."}),
    row("Sign the helper without Xcode (paid team)", PH,
        ["signing apply", "signing card", "signing export", "signing import", "signing resolve", "signing status"],
        ["setup"], [], no={"mcp": "It writes to the person's Apple team, so the person approves each request."}),
    # -- Tasks and agents ------------------------------------------------------------------------------------------
    row("Run a task", TA, ["run", "chat", "demo", "tui"], ["runs", "threads", "harness", "prewarm", "events", "home",
                                                          "inbox", "engine", "agents"],
        ["phone_task", "phone_task_status"]),
    row("Stop a task", TA, [], ["runs"], ["stop"], no={"cli": "ctrl+c stops a task in the terminal."}),
    row("Past tasks and exports", TA, ["history", "export"], ["runs", "export", "attachments", "transfers", "queue"],
        [],
        gap={"mcp": (3, "An agent can read one task's status but not list or export past tasks.")}),
    row("Memory", TA, ["memory add", "memory clear", "memory export", "memory list", "memory pin", "memory rm",
                       "memory unpin", "memory keep", "memory forget"], ["memory"], [],
        gap={"mcp": (3, "An agent can't read or edit what Mobster remembers.")}),
    row("Workflows", TA, ["workflow show"], ["workflows", "triggers", "groups", "replay"],
        ["list_workflows", "run_workflow", "graph_status"]),
    row("Approvals and permissions", TA, [], ["approvals", "permissions"], [],
        no={"cli": "Approvals are answered in the app or the terminal UI.",
            "mcp": "An agent never answers its own approval."}),
    row("Profiles and accounts", TA, [], ["accounts", "agents"], ["list_accounts", "list_profiles"],
        gap={"cli": (3, "No command lists profiles.")}),
    row("Alerts to a webhook", TA, ["alerts test"], ["triggers"], [], no={"mcp": "The webhook is the person's."}),
    row("Longform and Fleet", TA, [], ["longform", "fleet"], [],
        gap={"cli": (3, "Only the app runs these."), "mcp": (3, "An agent can't start these.")}),
    row("Teams, remote, plugins and destinations", TA, ["audit verify"],
        ["teams", "remote", "plugins", "connectors", "outlets", "waiting", "feedback"], [],
        no={"mcp": "These are the person's accounts and destinations."}),
    # -- Apps ------------------------------------------------------------------------------------------------------
    row("App packs: read and act in an app", AP, ["api serve", "api tokens"], ["packs", "apps"],
        ["instagram_*", "tiktok_*", "x_*", "youtube_*", "threads_*", "reddit_*", "linkedin_*", "notes_*",
         "messages_*", "maps_*", "photos_*", "safari_*", "settings_*", "calendar_*", "ios_docs"]),
    # -- Setup and account -----------------------------------------------------------------------------------------
    row("Connect an agent (MCP clients)", SU, ["mcp", "mcp clients", "mcp doctor", "mcp install"], ["integrations"], [],
        no={"mcp": "It is how MCP gets configured."}),
    row("Serve the local API", SU, ["serve"], [], [], no={"api": "It is the API.", "mcp": "It is the API."}),
    row("Keys and sign-in", SU, ["login", "logout"], ["keys", "ai-plans", "usage", "settings"], [],
        no={"mcp": "Sign-in and keys are the person's."}),
    row("Install, update and help", SU, ["update", "version", "completion", "help", "build-ocr"], [], [],
        no={"api": "The Mac app updates itself.", "mcp": "An agent has no use for them."}),
)

# Parser paths that are not capabilities of their own (a parent that only holds subcommands).
PARENTS = frozenset({"sim", "ci", "account", "account ci-token", "expo", "asc", "testflight", "xcode", "phone",
                     "memory", "env", "wifi", "workflow", "xray", "alerts", "audit", "api", "mcp", "phone clipboard",
                     "links", "fixtures", "signing"})


def matches(pattern, name):
    return fnmatchcase(name, pattern)


def analyse(cli, api, mcp):
    """Check ``ROWS`` against the surfaces: ``cli`` is the set of command paths, ``api`` the set of route paths (without
    the method, with /api/), ``mcp`` the set of tool names. Returns the list of problems, each one sentence."""
    problems = []
    api_groups = {path.split("/")[2] for path in api if path.count("/") >= 2}
    listed_cli = {item for r in ROWS for item in r.cli}
    listed_api = {item.split("/")[0] for r in ROWS for item in r.api}
    for path in sorted(cli - listed_cli - PARENTS):
        problems.append(f"The command `mobster {path}` is in no row of mobile_agent/parity.py. Add it with its API and "
                        "MCP surfaces, or the reason it has none.")
    for name in sorted(n for n in mcp if not any(matches(p, n) for r in ROWS for p in r.mcp)):
        problems.append(f"The MCP tool {name} is in no row of mobile_agent/parity.py.")
    for group in sorted(api_groups - listed_api):
        problems.append(f"The API group /api/{group} is in no row of mobile_agent/parity.py.")
    for r in ROWS:
        for item in r.cli:
            if item not in cli:
                problems.append(f"{r.name}: `mobster {item}` is not a command.")
        for item in r.api:
            if not any(path == "/api/" + item or path.startswith("/api/" + item + "/") for path in api):
                problems.append(f"{r.name}: /api/{item} is not a route.")
        for pattern in r.mcp:
            if not any(matches(pattern, n) for n in mcp):
                problems.append(f"{r.name}: {pattern} is not an MCP tool.")
        for surface in SURFACES:
            has = bool(getattr(r, surface))
            recorded = surface in r.no or surface in r.gap
            if has and recorded:
                problems.append(f"{r.name}: {surface} has surfaces and a recorded "
                                f"{'reason' if surface in r.no else 'gap'}.")
            if not has and not recorded:
                problems.append(f"{r.name}: no {surface} surface and no reason. Add one to `no` or a ranked `gap`.")
            if r.group == "Testing" and surface == "mcp" and surface in r.gap:
                problems.append(f"{r.name}: a Testing row has no open MCP gap. Build it, or write why not in `no`.")
        for surface, value in r.gap.items():
            if not (isinstance(value, tuple) and value[0] in (1, 2, 3) and value[1]):
                problems.append(f"{r.name}: the {surface} gap needs a rank 1 to 3 and a sentence.")
    names = [r.name for r in ROWS]
    problems += [f"Two rows are called {n!r}." for n in sorted({n for n in names if names.count(n) > 1})]
    return problems


def gaps():
    """[(rank, row, surface, what)] of the open gaps, the testing ICP's first."""
    out = [(value[0], r, surface, value[1]) for r in ROWS for surface, value in r.gap.items()]
    return sorted(out, key=lambda item: (item[0], GROUPS.index(item[1].group), item[1].name, item[2]))
