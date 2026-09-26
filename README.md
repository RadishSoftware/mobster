# Mobster CLI

Mobster CLI is the open-source agent and command line that [Mobster](https://mobster.dev), the Mac app, is built on. It is an iPhone agent that works from the accessibility tree instead of screenshots. Each step reads the tree through WebDriverAgent (WDA) over USB. [Jev](https://docs.typesafe.ai/introduction), a typed decision model, then picks the operation, the target, whether the goal is met and whether the task is blocked, all in one call. A small helper model runs only when text has to be written, an answer extracted, a recovery hint given or a multi-app request compiled into a short plan (`plans.py`). Routes, replays and the decision memo take some steps with no model call at all.

The default step sends no screenshot. Images leave the Mac only when a task needs them: crops of on-screen pictures go to your helper model when a task asks about pictures (`vision_judge.py`, `visual_answer.py`), and the optional frontier-model policy (`frontier.py`, used by the iOSWorld harness) sends a small screenshot each turn.

Text answers must be grounded. Each non-null extracted value has to quote a literal that was actually seen on the screen. When no such literal exists, the value is `null` with the status `insufficient_evidence`, or the run ends as "Needs review". Answers about pictures are the vision model's judgment, not a citation, and the frontier policy's answer is its own final text.

## Requirements

- A Mac with full Xcode (not only the Command Line Tools), signed in to an Apple ID. A free personal team works, but iOS then asks you to rebuild the WDA runner every 7 days.
- Python 3.12 or later. The `python3` that ships with macOS is 3.9 and cannot install the dependencies: `brew install python@3.12`, or `uv python install 3.12`.
- `brew install libimobiledevice` (`idevice_id`, `ideviceinfo`, `iproxy`) and `git`.
- An iPhone with Developer Mode on, connected by USB, with "Trust This Computer" accepted.
- A Jev API key from [TypeSafe](https://docs.typesafe.ai/introduction) (`TYPESAFE_API_KEY`). A helper-model key is optional.

**Tested on:** one iPhone 15 Pro running iOS 26.0.1 over USB, plus iOS simulators for the iOSWorld harness only. Other iPhone models and iOS versions are untested, and nothing in the code gates on them.

## Quick start

From a checkout:

```sh
make venv                                   # python3.12 -m venv mobile_agent/.venv + pinned requirements
mobile_agent/.venv/bin/python -m mobile_agent --version
mobile_agent/.venv/bin/python -m mobile_agent demo       # offline fixture replay: no phone, no keys
```

If `python3.12` is not on your `PATH`, pass it: `make venv PYTHON_BOOTSTRAP=$(brew --prefix python@3.12)/bin/python3.12`, or with uv: `uv venv --python 3.12 mobile_agent/.venv && uv pip install --python mobile_agent/.venv/bin/python -r mobile_agent/requirements.txt`.

Make a private env file with `cp mobile_agent/.env.example mobile_agent/.env && chmod 600 mobile_agent/.env`, and fill in `TYPESAFE_API_KEY` with an editor (not on the command line, which keeps it in your shell history). `.env` files are gitignored; never commit one. Get WDA serving on `127.0.0.1:8100`, either with the managed setup API (`serve --manage-device`, [setup-api.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/setup-api.md)) or by hand ([usb-wda.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/usb-wda.md); `scripts/usb-wda.sh` keeps it running). Then preview one decision without acting:

```sh
mobile_agent/.venv/bin/python -m mobile_agent run 'Open Search' --wda-url http://127.0.0.1:8100 --env-file mobile_agent/.env
#   --execute to act, --helper for the text model, --spend-cap-usd to bound model spend
```

The local API (loopback only, port 8765) is `python -m mobile_agent serve`; see [running.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/running.md). The optional helper model is configured with `TEXT_MODEL`, `TEXT_MODEL_PROVIDER`, `TEXT_MODEL_API_KEY` and `TEXT_MODEL_BASE_URL`. To use Vertex AI through an existing `gcloud` login, set `TEXT_MODEL_PROVIDER=vertex` together with `GOOGLE_CLOUD_PROJECT=<your-project>` and `GOOGLE_CLOUD_LOCATION=global`.

To run the tests (no phone, simulator or key needed):

```sh
make test-backend                           # mobile_agent/.venv/bin/python -m unittest discover -s mobile_agent -t .
```

### As a package

The distribution is named `mobster-cli` (`mobster` on PyPI is an unrelated project). It is not published to PyPI yet; from a checkout, `pip install .` into a Python 3.12 environment installs the `mobile_agent` package and a `mobster` command (`mobster --version`, `mobster run ...`, `mobster serve ...`). An installed copy keeps its task journal and compiled helpers in `~/Library/Application Support/app.mobster.desktop/`, never in site-packages. `pyproject.toml` declares compatible dependency ranges; `mobile_agent/requirements.txt` holds the exact tested pins.

## What is in the package

- the observe/decide/act loop (`agent.py`), with every mutation journaled before it is sent (`journal.py`);
- the WDA driver (`drivers.py`) and the managed USB iPhone lifecycle (`device_manager.py`, `setup_service.py`);
- the Jev decision path and helper models (`models.py`, `helper_models.py`);
- compiled loops over feeds (`loops.py`, `vision_judge.py`) and a frontier-model step policy (`frontier.py`);
- a loopback-only HTTP API (`server.py`);
- the evaluation harness (`evals/`), the MobsterBench-iOS benchmark and the iOSWorld runner (`bench/`).

**[Mobster](https://mobster.dev)**, the Mac app built on Mobster CLI, is a separate, closed-source product. It runs on Apple Silicon Macs only.

## Documentation

| Topic | Where |
| --- | --- |
| Framework: loop, API, results, safety, limits | [mobile_agent/README.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/README.md) |
| Design notes index | [mobile_agent/docs/](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/README.md) |
| Running `serve` and `run`, configuration, simulators | [mobile_agent/docs/running.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/running.md) |
| How a run works end to end | [mobile_agent/docs/architecture.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/architecture.md) |
| Benchmarks: MobsterBench-iOS and iOSWorld | [mobile_agent/docs/benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md), [mobile_agent/bench/README.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/bench/README.md) |
| Setup and device API | [mobile_agent/docs/setup-api.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/setup-api.md) |
| Manual USB and WDA setup | [mobile_agent/docs/usb-wda.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/usb-wda.md), `scripts/usb-wda.sh` |
| Compiled intent (routes and plans before the step model) | [mobile_agent/docs/compiled-intent.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/compiled-intent.md) |
| Saved tasks and schedules | [mobile_agent/docs/workflows.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/workflows.md) |

## Status and current results

All results below come from one physical iPhone 15 Pro running iOS 26 over USB, unless a line says otherwise. Every task is graded by an independent oracle, either its own WDA reads of the device or ground truth fixed before the run. The oracle never reads the agent's evidence. The run reports behind these numbers are internal; each line gives its conditions.

**Oracle-graded task suites, 22 Sep 2026:**

- The Settings suite (7 tasks, 5 repeats) passed 35/35.
- The full suite (14 Settings and Safari tasks, 3 repeats) passed 39/42 and 38/42 across two runs.
- Abstention tasks passed 9/9 in each of those runs: the agent declined to invent facts that are not on the device.
- An accessibility read takes about 130–210 ms. A Settings retrieval takes about 8 s end to end.

**MobsterBench-iOS, 24 Sep 2026: a development result, not a held-out score.** Six successive development passes ran the same 68 pre-registered tasks with the `mobster-next` configuration, one repeat per pass, and failures in one pass guided the fixes before the next ([benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md#what-the-pass-runs-are)).

- The last three passes scored 42/67 (63%), 48/68 (71%) and 54/66 (82%) of graded attempts. The rise is partly fitting to this suite; a fresh 3-repeat run on frozen code is the number to quote, and it has not been run yet.
- The last pass took a median of 7.7 s per task (p90 17.1 s) and cost about $0.006 per task.
- Navigation and scrolling scored 100%. Multi-app tasks were the weakest category at 2/6.
- The last pass also blocked 1 risky tap and recorded 2 unintended actions. The benchmark requires 0 of each.
- **No baseline agent ran in these passes**, so the pre-registered state-of-the-art comparison has not been made.

**iOSWorld** (133 tasks over 26 apps on an iPhone 17 Pro simulator, published best 51.9%). The runner `python -m mobile_agent.bench.iosworld run` uses iOSWorld's own reset and LLM judge. In an 11-task smoke run on 24 Sep 2026, the Jev step policy passed none of the tasks, mostly because it kept waiting (recorded in `frontier.py`). The frontier-model policy (`--policy frontier`) was added in response. No full iOSWorld result has been published yet.

**Known gaps:**

- Apps that draw their state instead of exposing it (calculators, maps, canvases, games), and text inside images, are invisible to the tree.
- Chrome's hidden tab switcher leaks into the tree, so web tasks target Safari.
- Thresholds for accepting answers and detecting completion still need calibration data that includes wrong answers.
- "Ask before acting" pauses before actions Mobster recognizes as sending, buying, posting, deleting or submitting. It is a heuristic, not a guarantee: the side-effect thresholds are uncalibrated.
- The service is a single-operator runtime that listens only on loopback. It is not a hosted multi-user service.

## Data that leaves your Mac

Mobster has no server and sends nothing to Radish Retail, LLC. A run sends the task text and the screen's accessibility text to Jev (TypeSafe), and, when configured, text and image crops to the helper model provider you choose. Keys stay in your env file. Task journals stay on your Mac and contain task goals and observed screen text: do not publish them. Automating an app is subject to that app's own terms.

## License

MIT, copyright Radish Retail, LLC. See [LICENSE](https://github.com/uninstantiated/mobster-cli/blob/main/LICENSE). Third-party components and their licences are listed in [THIRD_PARTY_NOTICES.md](https://github.com/uninstantiated/mobster-cli/blob/main/THIRD_PARTY_NOTICES.md).
