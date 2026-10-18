# Design notes

Dated notes on how Mobster's engine works and how it was measured. Each note is as it was on its date; when two
disagree, the later one describes the code more accurately. Quick mode, the fast-model pipeline several of them
describe, was retired on 13 Oct 2026: the loop in `mobile_agent/frontier.py` is the engine now. Research write-ups
are in [`research/`](../../research/README.md).

| document | what it covers | date |
| --- | --- | --- |
| [architecture.md](architecture.md) | Perception, decision layers, guards, extraction and verification, and how a run flows; module and function map; environment variables | 2026-09-24 |
| [benchmarks.md](benchmarks.md) | MobsterBench-iOS and iOSWorld: what each measures, commands, and measured results, losses included | 2026-09-24, iOSWorld to 2026-09-28 |

Some notes link to files of the full Mobster that this repository doesn't hold.
