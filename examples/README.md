# Examples

Every example below runs as written. Try `embed_runtime.py` first: it needs no phone and no key.

Run the commands from the repository root. Where a command says `mobster`, `mobster` is on your `PATH`, or `mobile_agent/.venv/bin/python -m mobile_agent` after `make venv`.

| example | what it shows | needs | command |
| --- | --- | --- | --- |
| [embed_runtime.py](https://github.com/RadishSoftware/mobster/blob/main/examples/embed_runtime.py) | the terminal UI's engine from Python, with your own approval rule | `make venv`; nothing else, it runs on the scripted demo phone | `PYTHONPATH=. mobile_agent/.venv/bin/python examples/embed_runtime.py` |
| [run_and_check.sh](https://github.com/RadishSoftware/mobster/blob/main/examples/run_and_check.sh) | `mobster run` in a shell script: read the result with `jq`, branch on the exit code | the CLI, `jq`, an iPhone, a model key | `examples/run_and_check.sh "Turn on Dark Mode"` |
| [api_client.py](https://github.com/RadishSoftware/mobster/blob/main/examples/api_client.py) | a client for `mobster serve`: start a task, print its steps from the event stream, answer approvals | a running `mobster serve --enable-live`, an iPhone, a model key | `python3 examples/api_client.py --token-file "$TOKEN_FILE" settings "Turn on Dark Mode"` |
| [agents](https://github.com/RadishSoftware/mobster/tree/main/examples/agents) | Mobster's MCP server in the Claude Agent SDK, OpenAI Agents SDK, Vercel AI SDK and LangChain | the CLI; a key for the agent run, none for `--list` | `.venv/bin/python main.py --list` |
| [launchers](https://github.com/RadishSoftware/mobster/tree/main/examples/launchers) | start a task from Siri, Shortcuts, Raycast or Alfred through the running Mobster ([how](https://docs.mobster.dev/launchers)) | Mobster for Mac or `mobster serve` | `examples/launchers/mobster-task.sh "What's my battery level?"` |
| [workflows](https://github.com/RadishSoftware/mobster/tree/main/examples/workflows) | five saved tasks, saved paused | a running `mobster serve` or Mobster for Mac | `curl ... --data @examples/workflows/battery-storage.json .../api/workflows` |
| [dev](https://github.com/RadishSoftware/mobster/tree/main/examples/dev) | CI workflows for GitHub and GitLab, a launch check | a Mac runner | see its README |
| [plugins](https://github.com/RadishSoftware/mobster/tree/main/examples/plugins) | a plugin folder | Mobster for Mac | see its README |
| [ios/Daybreak](https://github.com/RadishSoftware/mobster/tree/main/examples/ios/Daybreak) | a sample app with checks | Xcode and a simulator | see its README |

## embed_runtime.py

Runs one task through the runtime and prints its steps. It asks your `policy` function before each approval; the one in the file approves everything except a Delete.

```sh
PYTHONPATH=. mobile_agent/.venv/bin/python examples/embed_runtime.py
```

Output, on the scripted demo phone with Mobster 0.4.0, 17 Oct 2026 (the milliseconds vary):

```text
❯ Text Alex 'running 10 minutes late'  (Messages, scripted demo)
  Tapped “Alex Rivera”  (743 ms)
  Typed into “Message”  (732 ms)
  approval: tap “Send” → yes
  Tapped “Send”  (781 ms)
Done: Sent “running 10 minutes late” to Alex Rivera.
```

It exits 0 when the task finished.

## run_and_check.sh

Runs a task with `mobster run --execute --json`, prints the `result` event, and exits with the run's exit code: 0 finished, 3 no phone answered. A task that finishes prints the `result` line (`status`, `reason`, `data`) and `Done. Events are in <file>`; this needs an iPhone and a model key, so the output below is the run with no phone.

```sh
examples/run_and_check.sh "Turn on Dark Mode"
```

Output, with nothing listening on `127.0.0.1:8100`, 17 Oct 2026 (exit code 3):

```text
No phone at http://127.0.0.1:8100. Run: mobster doctor
```

## api_client.py

Start `mobster serve`, which prints the path of its token file on its first line, then run the client with it:

```sh
mobster serve --wda-url http://127.0.0.1:8100 --enable-live --env-file "$MOBSTER_ENV_FILE" > /tmp/mobster-serve.log &
python3 examples/api_client.py --token-file "$(head -1 /tmp/mobster-serve.log | jq -r .tokenFile)" \
    settings "Turn on Dark Mode"
```

It prints each step and, at an approval, asks `Allow? [y/N]` at the terminal. Output of the same client against a server with no phone and no key, 17 Oct 2026 (exit code 1):

```text
Live tasks are off: Add OPENAI_API_KEY or ANTHROPIC_API_KEY to the agent's env file; Live runs are off; enable them to act on the phone; No iPhone attached: start the agent with --manage-device or --wda-url
```

The per-step output of a task on a phone is not recorded here yet.

[Scripts and the API](https://docs.mobster.dev/scripting) documents the JSON events, statuses, exit codes and HTTP endpoints these use.
