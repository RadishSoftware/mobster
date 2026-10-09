# The mobster command, for harnesses without MCP

If you can run shell commands but have no mobster MCP tools, use the CLI. It can't tap one element at a time the way the MCP tools can: on a simulator it runs checks, and on a phone it runs whole tasks with Mobster's own agent.

## Checks on the simulator

A launch-only check needs no key: Mobster installs and launches the app, opens a deep link if you give one, and checks the expectations.

```sh
mobster verify --app "$PWD/.mobster/build/Build/Products/Debug-iphonesimulator/<App>.app" \
  --open-url myapp://paywall --text "Choose your plan" --visible "id=plan_annual" --json
```

| Flag | Meaning |
|---|---|
| `--text TEXT`, `--no-text TEXT` | Text on screen, or not. Repeatable |
| `--visible SEL`, `--absent SEL` | `key=value[,key=value]` with `id`, `label`, `value`, `role`, `enabled`, `selected` |
| `--expect JSON` | Any assertion as JSON: `'{"count": {"id": "/^plan_/"}, "equals": 3}'` |
| `--check FILE` | Run a saved check (`.mobster/checks/<name>.yaml`) |
| `--launch-arg=ARG`, `--launch-env KEY=VALUE` | Launch arguments and environment |
| `--json` | One JSON object on stdout (`schema: "mobster.verify/1"`): `verdict`, `exit_code`, `summary`, `reason`, `assertions[]`, `report` |

Steps in plain English (`mobster verify "Tap Continue" --app …`) run only with the user's own OpenAI or Anthropic key (Smart). Without one, a check with steps is a usage error.

The exit code is the verdict: 0 passed, 1 failed, 2 needs review, 3 couldn't run.

## A real iPhone

| Command | What it does | JSON |
|---|---|---|
| `mobster devices --json` | Lists iPhones and simulators and their state | `{"devices": [{"id", "kind", "name", "state", "reason", …}]}` |
| `mobster screen --device NAME --out shot.png` | Saves a screenshot. Taps nothing | none; read the PNG |
| `mobster run "TASK" --device NAME --execute --json` | Runs the task with Mobster's agent on the user's own Claude or OpenAI key | JSON lines; the last is the result |
| `mobster phone clipboard set TEXT --device NAME --json` | Sets the clipboard | `{"ok": true, …}` |
| `mobster phone install PATH --device NAME --json` | Installs a signed `.app` or `.ipa` | `{"ok", "device", "bundle_id", "seconds"}` |

`mobster run --execute` acts on the phone. Mobster's agent asks before it sends, buys, posts or deletes, and with `--json` nobody can answer, so it doesn't: the run ends as `approval_denied`. Show the user what you plan to run and add `--execute` only after they say yes. With no model key it exits 3: tell the user to run `mobster login`.

Exit codes for `run`: 0 finished, 1 didn't finish (the result's `status` says why), 2 usage, 3 couldn't run (no phone answered, or no key).

Full reference: https://docs.mobster.dev/cli
