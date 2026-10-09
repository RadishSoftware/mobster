# Setup and troubleshooting

Mobster runs on a Mac with Apple silicon, macOS 15 or later, and Xcode with an iOS Simulator runtime.

## Install and connect

```sh
curl -fsSL https://mobster.dev/install.sh | sh     # or: brew install radishsoftware/tap/mobster
mobster sim doctor --fix                           # once: creates the simulator and builds WebDriverAgent
mobster mcp install --all                          # adds the server to every agent found on this Mac
mobster mcp doctor                                 # starts the server, lists its tools, calls status
```

`mobster mcp install` writes the absolute path of `mobster` into each client's config, because apps opened from the Dock don't see your shell's `PATH`. `mobster mcp clients` shows which clients it found and where Mobster is set up. Restart a client after installing.

## Common problems

| Symptom | Fix |
|---|---|
| No mobster tools in the client | Run `mobster mcp clients`; install for that client, then restart it |
| The server fails to start | Run `mobster mcp doctor`; it shows the command the client runs and where it fails |
| `mobster: command not found` | Install Mobster, or use the absolute path `mobster mcp clients` prints |
| `verify_start` stays `preparing` | The first run boots the simulator and builds WebDriverAgent (about a minute). Keep calling `wait`, or run `mobster sim doctor --fix` beforehand |
| `couldnt_run`, class `environment` | Run `mobster sim doctor`; it prints the fix for each problem |
| A phone is `not_allowed` | The server was started with `--allow-device` for other phones. Ask the user |
| The phone is locked | Ask the user to unlock it. Mobster never enters a passcode |
| `verify` isn't listed | Smart needs the user's own key: `mobster mcp install --env-file PATH`, with `OPENAI_API_KEY=` or `ANTHROPIC_API_KEY=` in a file only they can read (`chmod 600`) |

Docs: https://docs.mobster.dev/mcp-server
