# Daybreak

Daybreak is a small SwiftUI app for a morning routine that ships with Mobster so you can try `mobster verify` on a real app in a few minutes. It has onboarding with a notification prompt, a three-plan paywall, a Today screen and Settings, in light and dark, plus four bugs you can plant to watch a check fail.

Everything in it is fictional. It has no packages, no StoreKit and no network access, and it sells nothing. The sunrise on its screens is drawn in SwiftUI (`Sky.swift`), so the app icon is its only image.

![Daybreak on an iPhone 17 Pro simulator, in light and dark: onboarding, the reminders page, the paywall with three plans, the paywall with the missing-plan bug showing two, Today, and Settings](https://github.com/RadishSoftware/mobster/raw/main/examples/ios/Daybreak/images/screens.jpg)

| | |
|---|---|
| Bundle ID | `dev.mobster.daybreak` |
| Requires | iOS 17 or later. Built and tested with Xcode 26.4 and the iOS 26.4 simulator |
| URL scheme | `daybreak` |

## Build it for the simulator

From the repository root:

```sh
xcodebuild -project examples/ios/Daybreak/Daybreak.xcodeproj -scheme Daybreak \
  -destination 'generic/platform=iOS Simulator' -derivedDataPath examples/ios/Daybreak/.build CODE_SIGNING_ALLOWED=NO build
```

It ends with `** BUILD SUCCEEDED **` and leaves the app at `examples/ios/Daybreak/.build/Build/Products/Debug-iphonesimulator/Daybreak.app`. No signing team is needed. `Daybreak.xcodeproj` is generated from `project.yml` by [XcodeGen](https://github.com/yonaskolb/XcodeGen) and committed, so you only need XcodeGen if you change `project.yml`.

## Run the sample checks

The checks live in `.mobster/checks/`. Each one names the app by the path above, relative to this folder.

| Check | How it runs | What it proves |
|---|---|---|
| `paywall.yaml` | Launch-only: opens `daybreak://paywall`, no model calls | The paywall shows three plans and Annual reads "$39.99 / year" |
| `onboarding.yaml` | Smart: Mobster taps through onboarding on your OpenAI or Anthropic key | A new user who declines notifications reaches the paywall |
| `reminder.yaml` | Key-less: your coding agent drives through `mobster mcp` | The daily reminder is still on after a relaunch |
| `today.yaml` | Launch-only: opens `daybreak://today`, no model calls | Today lists the three habits and the streak |
| `settings.yaml` | Launch-only: opens `daybreak://settings`, no model calls | Settings shows the daily reminder and "On this iPhone only" |

```sh
cd examples/ios/Daybreak
mobster verify --check .mobster/checks/paywall.yaml
```

The run exits 0 when every expectation holds, and writes a report with the frames to `.mobster/runs/<run_id>/report.html`. On 28 Sep 2026 it printed:

```
✓ passed  The paywall shows three plans with Annual at $39.99 a year  (27.9 s)
  ✓ text "Choose your plan"
  ✓ count id=/^plan_/ == 3
  ✓ value id=plan_annual == "$39.99 / year"
  ✓ visible label=Restore Purchases role=button
  ✓ no_text "Loading"
```

That run also started WebDriverAgent, which took 20.6 s of the 27.9. [Quickstart: test your app](https://docs.mobster.dev/quickstart/test) walks through the output, and [MCP server](https://docs.mobster.dev/mcp-server) shows `reminder.yaml` as MCP tool calls.

## Run them all

`mobster test` runs every check in `.mobster/checks` and writes one report:

```sh
cd examples/ios/Daybreak
mobster test --keyless                                     # the three launch-only checks, no key needed
mobster test --tag smoke --sim "iPhone 17 Pro" --sim "iPhone SE (3rd generation)" --parallel 2
mobster test --repeat 5 --tag smoke                        # each check five times: the pass rates
```

With a key, `onboarding.yaml` and `reminder.yaml` run too: Smart takes their steps. The report is `.mobster/test-results/<suite-id>/index.html`, with `junit.xml` beside it. [Run every check](https://docs.mobster.dev/testing) explains the flags.

To run the checks on your iPhone, build Daybreak for a device with your signing team and install it from Xcode, then pass the phone by name: `mobster test --device "Sam's iPhone" --tag smoke`. Mobster runs checks only on builds you put on the phone.

## Plant a bug

The paywall bug is one line of Swift. `scripts/plant-bug.sh` changes `ForEach(Plan.all)` to `ForEach(Plan.all.prefix(2))` in `Daybreak/PaywallView.swift`, so the Annual plan disappears:

```sh
scripts/plant-bug.sh missing-plan           # plant it
xcodebuild -project Daybreak.xcodeproj -scheme Daybreak -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build CODE_SIGNING_ALLOWED=NO build
mobster verify --check .mobster/checks/paywall.yaml
scripts/plant-bug.sh missing-plan --undo    # take it out, then run the xcodebuild line again
```

With the bug planted, the check exits 1. On 28 Sep 2026 it printed:

```
✗ failed  The paywall shows three plans with Annual at $39.99 a year  (18.2 s)
  ✓ text "Choose your plan"
  ✗ count id=/^plan_/ == 3: found 2: plan_weekly, plan_monthly
  ✗ value id=plan_annual == "$39.99 / year": not found; closest: "plan_monthly"
  ✓ visible label=Restore Purchases role=button
  ✓ no_text "Loading"
```

Every bug can also be switched on at launch, with no rebuild, through the launch argument `-DaybreakBug <name>`:

| Name | What goes wrong | The expectation that catches it |
|---|---|---|
| `missing-plan` | The paywall shows 2 plans instead of 3 | `count: {id: /^plan_/}`, `equals: 3` |
| `annual-price` | Annual reads "$39.99 / month" | `value: {id: plan_annual}`, `equals: $39.99 / year` |
| `reminder-not-saved` | The daily reminder isn't saved, so it's off after a relaunch | `value: {id: daily_reminder}`, `equals: true`, after `relaunch` |
| `stuck-loading` | The paywall shows "Loading plans…" and never loads | `no_text: Loading` |

```sh
mobster verify --app .build/Build/Products/Debug-iphonesimulator/Daybreak.app \
  --launch-arg=-DaybreakBug --launch-arg=annual-price --open-url daybreak://paywall \
  --expect '{"value": {"id": "plan_annual"}, "equals": "$39.99 / year"}'
```

Write `--launch-arg=-DaybreakBug` with the `=`: an argument that starts with `-` is otherwise read as a flag.

## Screens and identifiers

Checks and coding agents find controls by these accessibility identifiers.

| Screen | Control | Identifier | Label and value |
|---|---|---|---|
| Onboarding | Continue, on pages 1 and 2 | `onboarding_continue` | "Continue" |
| Onboarding | Turn on reminders, on page 3. It asks for notification permission | `onboarding_reminders` | "Turn on reminders" |
| Onboarding | Get started, on page 3. It opens the paywall | `onboarding_done` | "Get started" |
| Paywall | Title | `paywall_title` | "Choose your plan" |
| Paywall | Weekly plan | `plan_weekly` | "Weekly", value "$2.99 / week" |
| Paywall | Monthly plan | `plan_monthly` | "Monthly", value "$7.99 / month" |
| Paywall | Annual plan, selected by default, with the badge "Best value" | `plan_annual` | "Annual", value "$39.99 / year" |
| Paywall | Start free trial. It shows "Purchases are off in this sample" | `paywall_cta` | "Start free trial" |
| Paywall | Restore Purchases | `paywall_restore` | "Restore Purchases" |
| Paywall | Terms and Privacy links | none: select them by label | "Terms", "Privacy" |
| Paywall | Not now. It closes the paywall | `paywall_close` | "Not now" |
| Today | Habit switches | `habit_water`, `habit_read`, `habit_walk` | "Drink water", "Read 10 pages", "Walk 5,000 steps"; value `1` or `0` |
| Today | Streak | `streak_count` | "12-day streak" |
| Today | Settings button | `open_settings` | "Settings" |
| Settings | Daily reminder switch, saved with `@AppStorage` | `daily_reminder` | "Daily reminder"; value `1` or `0` |
| Settings | Reminder time | none | "Reminder time" |
| Settings | Show paywall | `settings_paywall` | "Show paywall" |

The selected plan carries the `Selected` trait, so `{id: plan_annual, selected: true}` matches it.

## Deep links and launch arguments

| Link or argument | Does |
|---|---|
| `daybreak://paywall` | Opens the paywall at once, even on the first launch |
| `daybreak://settings` | Opens Settings |
| `daybreak://today` | Opens Today |
| `-DaybreakSkipOnboarding YES` | Starts on Today instead of onboarding |
| `-DaybreakBug <name>` | Plants one of the bugs above for this launch |

`mobster verify --open-url` and a check's `launch.url` open a link after launch. To open one by hand, run `xcrun simctl openurl booted daybreak://settings`. The first link on each simulator makes iOS 26 ask "Open in “Daybreak”?", and the link waits until you answer. Once you press Open, iOS doesn't ask again on that simulator, even after the app is reinstalled.
