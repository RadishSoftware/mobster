"""Check steps for the system surfaces an app lives on outside its own window, driven and read on a simulator:
App Intents, Home Screen widgets, Live Activities, Control Center, App Clips, Spotlight and the share sheet.

Off unless ``MOBSTER_EXPERIMENTAL_SYSTEM_SURFACES=1`` (docs/checks.mdx). A surface is driven from outside the app with
WebDriverAgent and read from the accessibility tree like any screen, so the assertions are the check language's own.
``spec`` is the check file's block, ``play`` runs it after the flow. They are imported by name because the check
loader needs the first before the run machinery the second leans on exists."""
