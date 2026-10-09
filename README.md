# Mobster

Do anything on your iPhone with agents.

[Mobster for Mac](https://mobster.dev) puts an agent on your real iPhone. Type a task, watch every tap in a live view of the phone, and approve anything it would send, buy, post or delete. It does your own tasks in the apps you're already signed in to, and it tests the app you're building on your own phone and on simulators. It reads each screen's accessibility tree (the labelled controls VoiceOver reads), then taps, types, swipes and opens apps over USB from your Mac, with no cloud in between.

This repository is Mobster CLI, the engine inside Mobster for Mac. It's free and open source under MIT. Use it to try Mobster from Terminal, to give your coding agent a simulator or your iPhone, or to read exactly what touches your phone.

## Mobster for Mac

- **Guided setup for each phone.** Developer Mode, Xcode and Mobster's helper on the iPhone, one step at a time, with no Terminal. It signs the helper and renews it before a free Apple ID's 7 days run out.
- **Watch it work.** A live view of each iPhone, with approvals inline: Mobster shows the app and the exact text before it sends, buys, posts or deletes, and you can take over with a click.
- **Conversations that remember.** A follow-up knows what the last task found, Mobster remembers what you tell it, reads the files you attach, and listens when you hold the mic (macOS 26 or later).
- **Every phone you plug in.** Several iPhones and simulators in one window, each with its own live view, approvals and history, plus saved workflows and schedules.

Mobster for Mac is \$67 once (\$34.99 in launch week, through 15 October 2026) or \$7.67 a month, for every phone you plug in. Download it from [mobster.dev](https://mobster.dev), then follow [Quickstart: Mobster for Mac](https://docs.mobster.dev/quickstart/mac). There's no trial: the free CLI below is the way to try Mobster.

## Mobster CLI

The `mobster` command, its MCP server and the agent loop, from Terminal:

- **Test your app.** `mobster test` runs every check in your project on simulators and on your iPhone, with retries, flaky checks named, and JUnit and HTML reports. The verdict comes from assertions on the tree, never from a model.
- **Run your own tasks.** Mobster's agent works in a conversation that remembers what earlier tasks found and what you told it, reads files you attach, and asks before it sends, buys, posts or deletes.
- **Use the agent you already have.** `mobster mcp` gives Claude Code, Codex, Cursor or your own agent the phone, or hands a whole task to Mobster's agent with `phone_task`. [Integrations](https://mobster.dev/integrations) has a page for each agent.

Mobster drives iPhones over a USB cable and iOS simulators from an Apple silicon Mac. It doesn't drive Android, and it can't get past Face ID or a passcode.

## Install Mobster CLI

On an Apple silicon Mac with Xcode and an iOS Simulator runtime:

```sh
curl -fsSL https://mobster.dev/install.sh | sh
```

or with Homebrew, which installs the same build and needs no Command Line Tools:

```sh
brew install radishsoftware/tap/mobster
```

Then prepare the simulator once. This creates it, boots it headless and builds WebDriverAgent:

```sh
mobster sim doctor --fix
```

## Test your app

### A first check with the sample app

[Daybreak](https://github.com/RadishSoftware/mobster/tree/main/examples/ios/Daybreak) is a SwiftUI app for a morning routine, with a sunrise drawn in SwiftUI, onboarding, a three-plan paywall, a Today screen and Settings, in light and dark. Build it and check the paywall:

```sh
git clone https://github.com/RadishSoftware/mobster && cd mobster
xcodebuild -project examples/ios/Daybreak/Daybreak.xcodeproj -scheme Daybreak \
  -destination 'generic/platform=iOS Simulator' -derivedDataPath examples/ios/Daybreak/.build CODE_SIGNING_ALLOWED=NO build
cd examples/ios/Daybreak
mobster verify --check .mobster/checks/paywall.yaml
```

The check opens `daybreak://paywall` and expects three plans, Annual at "$39.99 / year", a Restore Purchases button and no loading text. It exits 0 and writes `.mobster/runs/<run_id>/report.html`. Now break the paywall with a one-line change and run it again:

```sh
scripts/plant-bug.sh missing-plan      # ForEach(Plan.all) becomes ForEach(Plan.all.prefix(2))
xcodebuild -project Daybreak.xcodeproj -scheme Daybreak -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build CODE_SIGNING_ALLOWED=NO build
mobster verify --check .mobster/checks/paywall.yaml
```

It exits 1 (output captured 28 Sep 2026):

```text
✗ failed  The paywall shows three plans with Annual at $39.99 a year  (18.2 s)
  ✓ text "Choose your plan"
  ✗ count id=/^plan_/ == 3: found 2: plan_weekly, plan_monthly
  ✗ value id=plan_annual == "$39.99 / year": not found; closest: "plan_monthly"
  ✓ visible label=Restore Purchases role=button
  ✓ no_text "Loading"
```

The report shows the paywall with the two plan cards outlined in red. `scripts/plant-bug.sh missing-plan --undo` puts the line back.

### Every check, on every device

`mobster verify` runs one check. `mobster test` runs all of them:

```sh
mobster test                                          # every check in .mobster/checks, on a simulator
mobster test --sim "iPhone 17 Pro" --sim "iPhone SE (3rd generation)" --parallel 2
mobster test --device "Sam's iPhone"                  # your own build, on your own iPhone
```

It prints a line per check and device (`✓ passed`, `~ flaky`, `✗ failed`), then the totals and the paths of `junit.xml` and an HTML report with each run's frames, under `.mobster/test-results/<suite-id>/`. A failure is tried again from a fresh app, and a check that passes on a retry is marked flaky. On your iPhone, a check drives only a build you installed, stays in that app, and never clears its data. [Run every check](https://docs.mobster.dev/testing) covers quarantine, `--repeat`, tags, shards and recording a check from a task, and [Running checks in CI](https://docs.mobster.dev/ci) has GitHub Actions recipes.

### Three ways to run a check

| Mode | Who performs the steps | Model calls |
|---|---|---|
| Launch-only | Nobody. Mobster launches the app, opens a deep link if given, and checks | None |
| Key-less | Your coding agent, through `mobster mcp` | None by Mobster |
| Smart | Mobster, on your Claude or OpenAI key | One per agent turn, with the run's spend capped by `--max-usd` (default $0.25) |

Launch-only and key-less runs send nothing anywhere. Smart sends each screen's image and accessibility text to Anthropic or OpenAI, whichever key it runs on.

### Exit codes

| Exit | Verdict | Means |
|---|---|---|
| 0 | passed | Every assertion held on one settled screen |
| 1 | failed | An assertion didn't hold, or the app wasn't in front |
| 2 | needs_review | Nothing decided the run: no assertions, or they already held before the flow ran |
| 3 | couldnt_run | The check is invalid, or this Mac couldn't run it |

`mobster test` uses the same codes across its checks: 1 if one failed, else 3 if one couldn't run, else 2 if one needs review, else 0. `mobster verify --json` prints the result as one JSON object. [Checks](https://docs.mobster.dev/checks) documents it, along with the check file and every assertion.

## Use it from your coding agent

```sh
mobster mcp install --all                  # Claude Code, Codex, Cursor and every other agent on this Mac
claude mcp add mobster -- mobster mcp      # Claude Code
codex mcp add mobster -- mobster mcp       # Codex
```

For Cursor, add `mobster mcp` to `.cursor/mcp.json`. Your agent gets three kinds of tools:

- **Check a screen.** `verify_start` with your app and what must be true, then `screen`, `tap`, `type_text` and `swipe`, then `verify_finish` for the verdict. This needs no model key: your agent does the driving, and Mobster judges. `run_tests` runs the project's saved checks on simulators and returns the summary and the report paths.
- **Drive a phone.** With a `device` from `list_devices`, the same tools drive a simulator or your USB iPhone directly. `--allow-device` keeps the server to the phones you name.
- **Hand over a task.** `phone_task` gives a whole task to Mobster's agent, in a conversation, through the running Mobster app or `mobster serve`. Mobster's agent asks you before it sends, buys, posts or deletes, and your agent can't answer for you.

With `--env-file` pointing at your Claude or OpenAI key, the server also offers `verify`, which runs a check's steps itself. [MCP server](https://docs.mobster.dev/mcp-server) has each client's setup, the tools, and an instruction to paste into `CLAUDE.md` or `AGENTS.md`.

## Run your own tasks on your iPhone

The same agent drives a real iPhone over USB. Type a task such as "Turn on Dark Mode" in the terminal UI (`mobster`) and it carries it out one step at a time, asking before it sends, buys, posts or deletes anything. That needs your Claude or OpenAI key (`mobster run` uses Quick mode, which needs `TYPESAFE_API_KEY`) and a phone set up for WebDriverAgent. `mobster --demo` tries it with a scripted phone, no key needed.

In `mobster chat`, tasks follow on from each other. With Mobster for Mac or `mobster serve` running, the conversation is the one the app shows; with neither, the task runs in this terminal:

```sh
mobster chat --new "Find my last message from Kate Bell"
mobster chat "Reply that I'm running 10 minutes late"
```

The second task gets your earlier words and what the first one found, so it knows who "her" is. A message you send while a task works steers it at its next step, and never approves anything. `mobster memory add "My gym is the one on 5th Street"` tells Mobster's agent something to remember, on this Mac, and `mobster phone file put menu.pdf --app com.apple.Pages` puts a file in an app's folder on the phone.

Beyond tapping and typing, it can enter a sign-in code sent by text or email without the model seeing it, read Notification Center, put text on the phone's clipboard, install your own signed builds (`mobster phone install`), and post to a webhook when a scheduled task fails or waits for you. It stops at once on a locked phone instead of trying to get past the lock screen. It won't enter a passcode, use Face ID, confirm App Store or Apple Pay purchases with the side button, or open apps you locked. [What Mobster can and can't do](https://docs.mobster.dev/capabilities) has the details.

[Mobster for Mac](https://mobster.dev), the app built on this CLI, adds guided setup for each phone, several phones in one window, a live view, conversations with approvals inline, memory you can edit, attachments, push-to-talk, history and schedules.

- [Quickstart: Mobster CLI](https://docs.mobster.dev/quickstart/cli): install, the demo, a first task
- [Device setup](https://docs.mobster.dev/device-setup): WebDriverAgent on a USB iPhone
- [Conversations](https://docs.mobster.dev/conversations), [Memory](https://docs.mobster.dev/memory) and [Files](https://docs.mobster.dev/files)
- [Terminal UI](https://docs.mobster.dev/tui), [Scripts and the API](https://docs.mobster.dev/scripting) and [Configuration](https://docs.mobster.dev/configuration)

## What it doesn't do yet

- The iPhone stays on a USB cable. Wi-Fi after one cable pairing is a preview that's off unless you turn it on, while it's tested on real phones ([Without the cable](https://docs.mobster.dev/wifi)).
- Checks don't heal themselves or replay cached steps: every run launches the app fresh, and a Smart step calls the model each time.
- `mobster verify` installs apps on the iOS Simulator only; on a USB iPhone it checks an app already installed there. `mobster test --device` also installs an `.ipa` or a device build you give it. Neither clears an app's data on a phone.
- `phone_task` needs the Mobster app or `mobster serve` running. Memory goes with tasks from the Mac app, `mobster chat` and workflows, not yet with the terminal UI's.
- Voice is in Mobster for Mac only, on macOS 26 or later.
- iOS only.

## Documentation

- [Run every check](https://docs.mobster.dev/testing) and [Running checks in CI](https://docs.mobster.dev/ci): `mobster test`, devices, retries and flaky checks, reports, recipes
- [Quickstart: test your app](https://docs.mobster.dev/quickstart/test): install, the simulator, a first check and the report, then [steps and saved checks](https://docs.mobster.dev/test/steps)
- [Checks](https://docs.mobster.dev/checks): the check file, assertions, verdicts and the result JSON
- [MCP server](https://docs.mobster.dev/mcp-server): `mobster mcp` in Claude Code, Codex and Cursor, and its tools
- [Conversations](https://docs.mobster.dev/conversations): follow-ups, steering, `mobster chat`, `phone_task` and the threads API
- [Memory](https://docs.mobster.dev/memory) and [Files](https://docs.mobster.dev/files): what Mobster remembers, attachments and `mobster phone file`
- [Simulators](https://docs.mobster.dev/simulators): what Mobster creates, ports, WebDriverAgent, reset levels, `mobster sim`
- [CLI reference](https://docs.mobster.dev/cli): every command, flag and exit code
- [What Mobster can and can't do](https://docs.mobster.dev/capabilities): stopping on a locked iPhone (Mobster never unlocks it or enters a passcode), sign-in codes, notifications, the clipboard, installs, alerts, and what it refuses
- [Architecture](https://docs.mobster.dev/architecture) and [Troubleshooting](https://docs.mobster.dev/troubleshooting)
- [All docs](https://docs.mobster.dev), and for AI tools, [llms.txt](https://docs.mobster.dev/llms.txt)
- On mobster.dev: [Mobster for Mac](https://mobster.dev), [Integrations](https://mobster.dev/integrations), [Compare](https://mobster.dev/compare) and the [Blog](https://mobster.dev/blog)

## Support

Open an [issue](https://github.com/RadishSoftware/mobster/issues) with the output of `mobster sim doctor` and `mobster version`. Leave out screenshots of private screens. Report security problems privately, as [SECURITY.md](https://github.com/RadishSoftware/mobster/blob/main/SECURITY.md) describes.

## Contributing

Bug fixes and docs changes are welcome. [CONTRIBUTING.md](https://github.com/RadishSoftware/mobster/blob/main/CONTRIBUTING.md) covers the development setup, tests and pull requests. The test suite needs no simulator, phone or key.

## License

MIT, copyright Radish Retail, LLC. See [LICENSE](https://github.com/RadishSoftware/mobster/blob/main/LICENSE). Third-party components and their licences are listed in [THIRD_PARTY_NOTICES.md](https://github.com/RadishSoftware/mobster/blob/main/THIRD_PARTY_NOTICES.md).
