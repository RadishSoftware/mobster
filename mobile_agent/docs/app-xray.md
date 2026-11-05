# App X-ray (spike 14 Oct 2026, shipped 15 Oct 2026)

You point Mobster at an app, and it explores the app on its own with no model. It hands back a map of what it found:
- every screen it reached, with a thumbnail and its text;
- how the screens connect, each link labelled with the control it tapped;
- the paywalls it found, with their plans and prices;
- the stats.

Run it again after the app updates, and `diff` tells you what changed.

## Shipped (15 Oct 2026)

App X-ray is a feature now. The experimental flag and `python -m mobile_agent.xray` are gone.

- **Track `xray`** (`xray/register.py`, in `tracks.TRACKS`): the run field `xray` (`{"screens": 1..120, "minutes": 1..30}`, defaults 30 and 5), a pre-run hook (`xray/job.py`) that runs `Crawler` on the driver the Runtime already opened and ends the run with `PreRun(result=...)`, and `POST /api/xray/app`.
- **An ordinary task.** `Runtime.create` admits it, so the plan gate, the phone's owner, the queue and Stop apply. Three changes keep it keyless: `check_ready` runs under the engine name `no_model` (the phone must be ready; no model key), `run_smart` opens no model client for it, and neither the plan question nor the long-task detector runs for it. A coding-agent engine is refused for it.
- **Journaled.** Each tap emits `action_started` before it is sent and `action_acknowledged` after; a refused send emits `action_not_dispatched`; a send that raised is never repeated. Each new screen emits a `step` with its capture, which the run view shows and the result's gallery points at.
- **Result.** An answer line, a `gallery` of screens, a `table` of paywall plans and a `markdown` block of stats. The files are in `<user data>/xray/apps/<bundle>/<YYYYmmdd-HHMMSS>/` (0700).
- **Safety added.** Phone, FaceTime, Shortcuts and Home are not mapped (`policy.ACTING_APPS`): a plain tap there acts. The check runs in the route, the command, the hook and the crawler. Screen text is masked with `secret_filter` in `policy.plain`, where every label is read.
- **Commands.** `mobster xray app APP` and `mobster xray diff BEFORE AFTER` (`xray/cli.py` lists its subcommands in `SUBCOMMANDS`). `xray app` needs a plan; `xray diff` doesn't.

Run it:

```
mobster xray app com.apple.Preferences --screens 60 --minutes 10
mobster xray diff before/map.json after/map.json
```

Docs: `docs/xray.mdx`, `docs/privacy.mdx` and the generated `docs/reference/cli.mdx`.

<!-- measured on the test device: main agent fills (coverage on three real apps: screens, links, depth, refused, time, paywalls) -->

## What it does

- **Explores** (`crawl.py`), breadth first, within caps on screens, depth and time. A screen is recognised by its title and the words that stay put; times, counts and prices are left out (`screens.py`). To go back it uses the back control, then a close control, then an edge swipe, then a relaunch and a replay of the path. It scrolls long lists for a few pages.
- **Taps only what is shaped like navigation** (`policy.py`). The policy fails closed. It never taps:
  - a control whose words name an act (the engine's commit words, the packs' `ACT_LABEL` and `NEVER`, and a crawler deny list: delete, sign out, subscribe, start trial, allow, continue, and more);
  - a switch, slider, picker or text field;
  - a cell that holds a switch;
  - the app's chrome.

  On iPhone Mirroring, where elements are text lines with no roles, it is stricter still. It taps only list-row-shaped lines and tab labels, never centred lines, sentences or short verbs.
- **Reads paywalls** (`paywall.py`). It recognises a paywall from its text: prices with a period, trials, "Restore Purchases". It reads each plan's price, period and trial. It never taps a paywall; it backs out.
- **Writes the map** (`report.py`): `map.json` (schema `mobster.xray/1`) and one self-contained `report.html` in Mobster's report style.
- **Compares two maps** (`diff.py`): screens added, removed and changed, and paywall price changes.

## Measured (14 Oct 2026, this Mac, load average 31 to 38, `nice -n 10`)

The run was Settings (`com.apple.Preferences`) on one iPhone 17 Pro simulator, iOS 26.4, over WebDriverAgent. The caps were 60 screens and 6 minutes.

| Measure | Value |
|---|---|
| Screens mapped | 47 |
| Links | 47 |
| Deepest | 2 taps |
| Time | 6 min 01 s; it stopped at the time cap |
| Screens a minute | 7.8 |
| Taps | 65 |
| Controls refused by the policy | 235 |
| Model cost | $0 |

Of the 361 s, 213 s went to waiting for screens to settle, 47 s to launches and relaunches, and 28 s to taps. Settling is the first thing to speed up: a shorter settle on tree reads, using the WebDriverAgent animation-idle signal.

## What it doesn't do yet

- It reads no icons on Mirroring. Controls that show only an icon stay unread until a vision pass names them. MIRRORING.md measured OCR grounding 67% of tree targets.
- It types nothing. A screen behind sign-in or a search is mapped only up to that point.
- It answers no system dialogs except a clear "Not Now", "Cancel" or "Close".
- It has no schedule.
- Its first run on a real phone is a QA-queue request.

## Next

1. Release watch: a scheduled X-ray of an app after each App Store update, with the diff as a result card ("new paywall; annual price up $10"). This belongs to the paid autopilot line.
2. A vision pass on Mirroring for icon-only controls, used only when OCR finds nothing to tap.
3. Export the map to Figma frames, or hand it to Claude Code as a spec.
4. A shorter settle: measure it first, then change it.
