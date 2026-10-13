# App coverage: how much of each app the accessibility tree exposes

USB iPhone 15 Pro, iOS 26.0.1, 22 Sep 2026. `python -m mobile_agent.evals.app_survey`
launches each app (no taps, typing or scrolling), reads the tree the agent reads,
and compares it with on-device OCR of one screenshot. **Text recall** is the
share of visible text lines (status bar excluded) that the tree also carries.
Everything ran locally; only counts are kept (`app-coverage-2026-09-22.json`).
The survey ran on a personal phone, so apps that would identify its owner are
named by category only (`redacted.<category>` in the JSON).

## The biggest finding: our own depth cap

`snapshotMaxDepth` was 25. That limit dated from full-depth home-screen reads
taking 40 s while WDA's `visible` attribute was still requested. Without that
attribute the home screen reads in ~270 ms even at depth 80, and depth 25 was
hiding deep view trees: **Spotify's text recall went from 0.23 to 0.77**
(41 → 83 elements) at depth 50, for +70 ms. The depth is now 60.

## Results (30 apps with valid reads)

The phone locked partway through the sweep; 51 rows that were not read in the
foreground and 9 errors are excluded. These first-screen numbers are a
snapshot, not a verdict on any app.

| class | apps |
| --- | --- |
| good (≥0.85) | Health, App Store, a payments app, SeatGeek, JetBlue, a robotaxi app, TestFlight, a fitness-wearable app, Signal, Reminders, a building-access app |
| partial (0.6–0.85) | Uber, a banking app, LinkedIn, Snapchat, an email app, a workout-log app, a laundry-payment app, a productivity app |
| poor (<0.6) | Instagram 0.27, Reddit 0.25, Google Maps 0.22, Discord 0.44, Calculator 0.50, Weather 0.56, Transit 0.50, United 0.57, a shopping app 0.50, X (read during load) |
| opaque | a photo-social app (a drawn surface: 3 elements, 34 OCR lines) |

Reading the classes:

- **Feed apps** (Instagram, Reddit) show text inside images and video that no
  accessibility tree carries, so their recall understates what a person could
  act on.
- **Map canvases** (Google Maps) and **drawn displays** (Calculator's result)
  are real blind spots. That is the vision-fallback case in the frontier notes.
- **Discord** (React Native) is partially exposed. More RN apps are needed
  before generalising.
- **Unlabeled controls** (icon buttons with no label) run from 0 to 50 per
  screen (an email app 50, Uber 21, SeatGeek 18). These are invisible to a
  label-driven agent unless it uses geometry or vision.

## What this implies

1. Accessibility-first covers most mainstream native, React Native and web
   content. Its ceiling is set by canvases, drawn state, text inside images
   and unlabeled icons.
2. The general fix is a gated vision fallback: OCR or a grounding model on the
   regions the tree does not cover. It runs only when recall is low, so
   high-coverage apps keep the fast path. Crop provenance must stay
   distinguishable, and recognized text never becomes an instruction.
3. The survey is the regression gate: rerun it after any perception change.
