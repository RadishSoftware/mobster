# Research

Dated write-ups from building Mobster's engine: what we tried, how we measured it, and what lost. Each note is as it was written on its date; later changes to the engine are in the design notes in [`mobile_agent/docs/`](../mobile_agent/docs/README.md). Scripts named `research.*` in these notes are not published.

| Note | What it covers |
|---|---|
| [frontier-2026-09-22](frontier-2026-09-22.md) | A literature scan of mobile agents and what was built from it (suite 30/42 to 39/42 and 38/42) |
| [agent-frontier-2026-09-23](agent-frontier-2026-09-23.md) | Fewer model calls and faster steps: text as selection, speculation, same-screen completion; a same-day A/B and a negative result on scrolling |
| [app-coverage-2026-09-22](app-coverage-2026-09-22.md) | How much of 30 apps the accessibility tree exposes, measured against OCR; the depth cap was hiding content |
| [usb-speed-precision-2026-09-22](usb-speed-precision-2026-09-22.md) | A USB iPhone speed and precision pass; Settings suite 21/21 to 35/35 with per-task times |
| [web-suite-2026-09-22](web-suite-2026-09-22.md) | What real Safari pages broke and the general fixes; 15/21 web tasks, failures included |
| [latency-breakdown-2026-09-23](latency-breakdown-2026-09-23.md) | Where wall-clock time goes over 346 runs, and the top 10 speedups ranked |
| [frameclock-2026-09-23](frameclock-2026-09-23.md) | The MJPEG stream as a settle clock, the exact-distance glide scroll, SCROLL_TO, launch readiness |
| [vision-judge-2026-09-23](vision-judge-2026-09-23.md) | Calibrated visual yes/no answers on crops (Apple Vision, then a small model, then a second look) on 135 labelled images |
| [bench-2026-09-24](bench-2026-09-24.md) | MobsterBench-iOS pass 6: 68 tasks on an iPhone 15 Pro |
| [static-ax-gap-2026-09-21](static-ax-gap-2026-09-21.md) | Apps whose state is drawn rather than in the tree (Calculator), and a visual fingerprint for change detection |
| [precision-guard-audit-2026-09-19](precision-guard-audit-2026-09-19.md) | The action verifier added after an alarm-setting failure, its eval with unsafe acceptances, and the effect ledger |
| [output-field-eval-2026-09-19](output-field-eval-2026-09-19.md) | An answer-field binding regression (15/18) and the per-field claim check added for it |
| [extraction-quality](extraction-quality.md) | A failure taxonomy for answer extraction (21 and 22 Sep 2026), and a live lift from 50.0% to 69.0% |
| [hybrid-architecture](hybrid-architecture.md) | The fast-model and helper split of 21 Sep 2026, its escalation rules, and the eval numbers |

Quick mode, the fast-model pipeline several of these notes measure, was retired on 13 Oct 2026; the loop in `mobile_agent/frontier.py` is the engine now.
