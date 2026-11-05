---
description: Test the screens this branch changed with Mobster, and report the verdict with frames and GIFs
argument-hint: "[--device NAME] [--base REF]"
---

Test this PR with Mobster: $ARGUMENTS

1. Call the mobster `run_tests` tool with `pr: true` and `path` set to the absolute path of the project (the folder holding `.mobster`, or the repository's top folder). Pass `base` if the arguments name one. Pass `device` only if the arguments name an iPhone; otherwise it runs on simulators.
2. If it returns `running`, call `run_tests` with its `suite_id` until it finishes.
3. Report the headline, then each check that didn't pass: what it expected, what it found, and its frame and GIF paths. Show the failing frame.
4. For each draft in `drafts`, read the file. If its expectations match what the change was meant to do and it passed, offer to move it into `.mobster/checks/`. Never move a draft that failed, and never weaken an expectation to make a check pass.
5. On a failure, offer to fix the code and run `run_tests` with `pr: true` again, at most three times.
6. Give the path of `pr-comment.md`, the comment to paste on the pull request.

If `run_tests` says the iPhone or the TestFlight app isn't allowed, show the user that sentence and stop. Don't retry on another device.

If no mobster tools are available, run `"${CLAUDE_PLUGIN_ROOT}/scripts/mobster-mcp" --check` and show the user what it prints.
