# Examples

[ios/Daybreak](https://github.com/RadishSoftware/mobster/tree/main/examples/ios/Daybreak) is a SwiftUI sample app with a three-plan paywall, sample checks for `mobster verify`, and bugs you can plant to watch a check fail.

[agents](https://github.com/RadishSoftware/mobster/tree/main/examples/agents) connects Mobster's MCP server to the Claude Agent SDK, the OpenAI Agents SDK, the Vercel AI SDK and LangChain.

[launchers](https://github.com/RadishSoftware/mobster/tree/main/examples/launchers) starts a task from Siri and Shortcuts on the Mac or iPhone, Raycast or Alfred, through the running Mobster. [Start tasks from Siri, Shortcuts, Raycast or Alfred](https://docs.mobster.dev/launchers) sets each one up.

The scripts below drive a real iPhone through `mobster run` and `mobster serve`:

| file | what it shows | needs a phone |
| --- | --- | --- |
| [run_and_check.sh](https://github.com/RadishSoftware/mobster/blob/main/examples/run_and_check.sh) | `mobster run` in a shell script: preview, act, read the result with `jq`, and branch on the exit code | yes |
| [api_client.py](https://github.com/RadishSoftware/mobster/blob/main/examples/api_client.py) | a client for `mobster serve`: start a task, print its steps from the event stream, and answer approvals at the terminal | yes |
| [embed_runtime.py](https://github.com/RadishSoftware/mobster/blob/main/examples/embed_runtime.py) | the terminal UI's engine from Python, with your own approval rule; runs offline on the scripted demo phone by default | no |

Try the offline one first:

```sh
python examples/embed_runtime.py
```

```
❯ Text Alex 'running 10 minutes late'  (Messages, scripted demo)
  Tapped “Alex Rivera”  (287 ms)
  Typed into “Message”  (336 ms)
  approval: tap “Send” → yes
  Tapped “Send”  (345 ms)
Finished, unverified: Mobster judged the task done. Nothing checked it independently.
```

[Scripts and the API](https://docs.mobster.dev/scripting) documents the JSON events, statuses, exit codes and HTTP endpoints these use.
