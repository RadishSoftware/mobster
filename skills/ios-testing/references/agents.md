# Add Mobster to an editor or agent, in one line

Each line adds the `mobster` MCP server and the skill. It needs Mobster for Mac in Applications, opened once. Run it in a terminal, then start a new session in the agent (restart Cursor and Claude Desktop). The long path works before `mobster` is on `PATH`.

| Agent | One line |
|---|---|
| Claude Code | `/plugin marketplace add RadishSoftware/mobster-plugin` then `/plugin install mobster@mobster` |
| Cursor | `/Applications/Mobster.app/Contents/MacOS/mobster-agent mcp install cursor --with-skill` |
| Codex | `/Applications/Mobster.app/Contents/MacOS/mobster-agent mcp install codex --with-skill` |
| Windsurf | `/Applications/Mobster.app/Contents/MacOS/mobster-agent mcp install windsurf --with-skill` |
| Xcode's agents (Claude Agent and Codex inside Xcode) | `/Applications/Mobster.app/Contents/MacOS/mobster-agent mcp install xcode --with-skill` |
| Every agent on this Mac | `/Applications/Mobster.app/Contents/MacOS/mobster-agent mcp install --all --with-skill` |

Add `--project .` to write one repository's config (`.cursor/mcp.json`) instead of the user's. Xcode's agents have no project config. `mobster mcp doctor` starts the server the way the agent would and lists its tools. Only the skill, for any agent that reads Agent Skills: `npx skills add RadishSoftware/mobster-plugin`.

Xcode has its own simulator tools. Use them to build and to look at the app; use Mobster to check declared assertions, replay saved checks or test on a real iPhone. No model grades the outcome. Replay may call a model to heal a changed step or write generated text again. In Xcode, `mobster mcp install claude-code codex --with-xcode-tools` also gives those agents Xcode's own tools.

## Keep the loop in the repository

Put this in `AGENTS.md`, `CLAUDE.md` or `.cursor/rules`, so every agent that opens the project runs the loop without being asked:

```markdown
After you change a screen, verify it with mobster's verify_start and verify_finish. When it passes, call save_check, and commit the file in .mobster/checks. Before you open a pull request, call run_tests with pr: true and report its verdict.
```

## Run the saved checks on every pull request

`mobster ci` runs the checks that cover a pull request and posts the verdict and each failure's video on it. It runs on a Mac of the user's with Mobster for Mac: https://docs.mobster.dev/ci.
