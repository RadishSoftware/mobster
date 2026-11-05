---
description: Verify an iOS UI change on Mobster's simulator and report the verdict
argument-hint: "[what must be true, such as: the paywall shows three plans]"
---

Verify this iOS change with Mobster: $ARGUMENTS

Follow the ios-testing skill's verify loop:

1. Find the app's Xcode project or workspace and its scheme. Build it for the simulator:
   `xcodebuild -scheme <Scheme> -destination 'generic/platform=iOS Simulator' -derivedDataPath .mobster/build CODE_SIGNING_ALLOWED=NO build`
2. Turn the request above into `expect` assertions that hold only after the steps. If it's empty, check the UI you changed most recently in this session.
3. Call the mobster `verify_start` tool with the absolute `app_path`, the steps in plain English and `expect`. If it returns `preparing`, call `wait`.
4. Drive the app with `screen`, `tap`, `type_text`, `swipe`, `alert` and `open_url`, then call `verify_finish`.
5. On `failed`, fix the code, rebuild and verify again, at most three times. Never weaken an assertion to make it pass.
6. Report the verdict, the failing assertions if any, and the report path.

If no mobster tools are available, run `"${CLAUDE_PLUGIN_ROOT}/scripts/mobster-mcp" --check` and show the user what it prints.
