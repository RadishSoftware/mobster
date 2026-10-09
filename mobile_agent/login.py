"""`mobster login`, `mobster login status` and `mobster logout`: your model key, saved for Mobster on this Mac.

A thin front over keys.py, the same code Mobster for Mac's Setup uses: the key is tested with one tiny request
(``Keys.test``: a read of the model, then the smallest generation, which catches an account with no credit), then
saved in the same private settings file the Mac app uses (``config.app_env_file()``, mode 0600) through
``Keys.update``. So a key saved here works in Mobster for Mac, and the Mac app's key works here.

A key is never printed: only its first characters and last four (``mask``).
"""

import json
import os
import sys

PROVIDERS = {
    "anthropic": {"name": "Claude", "company": "Anthropic", "variable": "ANTHROPIC_API_KEY", "prefix": "sk-ant-",
                  "console": "the Claude Console", "url": "https://platform.claude.com/settings/keys"},
    "openai": {"name": "OpenAI", "company": "OpenAI", "variable": "OPENAI_API_KEY", "prefix": "sk-",
               "console": "the OpenAI Platform", "url": "https://platform.openai.com/api-keys"},
    "jev": {"name": "Jev", "company": "TypeSafe", "variable": "TYPESAFE_API_KEY", "prefix": "",
            "console": "TypeSafe", "url": "https://typesafe.ai"},
}
ORDER = ("anthropic", "openai", "jev")


def mask(key):
    """sk-ant-…a1F2: the key's well-known prefix and its last four characters, never more."""
    key = (key or "").strip()
    if len(key) < 12:
        return "…" + key[-2:] if key else ""
    for prefix in ("sk-ant-", "sk-proj-", "sk-svcacct-", "sk-"):
        if key.startswith(prefix):
            return f"{prefix}…{key[-4:]}"
    return f"…{key[-4:]}"


def source_words(variable, env=None):
    """Where a saved key came from, in words: Mobster for Mac's settings, an env file, or your shell."""
    from .config import SOURCES
    source = SOURCES.get(variable)
    if source == "mac-app":
        return "from Mobster for Mac"
    if source == "env-file":
        return "from your env file"
    return "from your shell"


def run(args):
    if args.command == "logout":
        return logout(args)
    if getattr(args, "action", None) == "status":
        return status(args)
    return login(args)


def _paint(stream=None):
    from .style import palette
    return palette(stream or sys.stdout)


def _keys():
    from .config import app_env_file
    from .keys import Keys
    return Keys(app_env_file())


def _saved():
    """{variable: value} in the Mac app's settings file (not the shell's)."""
    from .config import app_env_file
    path = app_env_file()
    values = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() in {p["variable"] for p in PROVIDERS.values()} | {"MOBSTER_SMART_PROVIDER"}:
                values[name.strip()] = value.strip().strip("'\"")
    except (OSError, UnicodeDecodeError):
        pass
    return values


def status(args):
    """Masked rows: which keys Mobster can use, and which one its agent uses."""
    from .engines import model_provider, smart_model
    paint = _paint()
    rows = []
    for provider in ORDER:
        info = PROVIDERS[provider]
        key = os.environ.get(info["variable"], "").strip()
        rows.append({"provider": provider, "name": info["name"], "set": bool(key), "key": mask(key) if key else None,
                     "source": source_words(info["variable"]) if key else None})
    smart = model_provider(smart_model()) if any(r["set"] for r in rows[:2]) else None
    if getattr(args, "json", False):
        print(json.dumps({"keys": rows, "agent": smart, "model": smart_model() if smart else None}))
        return 0
    print(paint("Keys", "bold"))
    for row in rows:
        name = paint(row["name"].ljust(8), "accent")
        if row["set"]:
            note = "  Mobster's agent uses it" if row["provider"] == smart else (
                "  Quick mode" if row["provider"] == "jev" else "")
            print(f"  {paint('✓', 'green')} {name}{row['key'].ljust(16)}{paint(row['source'], 'tertiary')}"
                  + paint(note, "secondary"))
        else:
            hint = "  only for Quick mode" if row["provider"] == "jev" else ""
            print(f"  {paint('·', 'tertiary')} {name}{paint('not set', 'tertiary')}" + paint(hint, "tertiary"))
    if not any(row["set"] for row in rows):
        print("\nNo key yet. Run " + paint("mobster login", "accent") + ".")
    return 0


def _choose(paint):
    """Ask which provider, Claude first. Returns anthropic or openai."""
    print(paint("Which AI do you use?", "bold"))
    print("  " + paint("1", "accent") + "  Claude, by Anthropic   " + paint("recommended", "green"))
    print("  " + paint("2", "accent") + "  OpenAI")
    while True:
        try:
            answer = input("Choose 1 or 2 [1]: ").strip()
        except EOFError:
            return None
        if answer in ("", "1", "claude", "anthropic"):
            return "anthropic"
        if answer in ("2", "openai"):
            return "openai"


def login(args):
    from .keys import KEY_PATTERN
    paint = _paint()
    provider = args.provider
    if args.with_key:
        if sys.stdin.isatty():
            print("mobster login: --with-key reads the key from a pipe or a file, not the keyboard: "
                  "mobster login --with-key < key.txt", file=sys.stderr)
            return 2
        key = sys.stdin.read().strip()
        provider = provider or _guess(key) or "anthropic"
    else:
        if not sys.stdin.isatty():
            print("mobster login: this asks for your key, so it needs a terminal. In a script: "
                  "mobster login --provider anthropic --with-key < key.txt", file=sys.stderr)
            return 2
        print("Mobster's agent runs on your own Claude or OpenAI key.")
        print("You pay them for what it uses, and the key stays on this Mac.")
        print()
        if provider is None:
            try:
                provider = _choose(paint)
            except KeyboardInterrupt:
                print()
                print(paint("Nothing was saved.", "tertiary"))
                return 130
            if provider is None:
                return 2
            print()
        info = PROVIDERS[provider]
        if provider != "jev":
            print(paint(f"Get a key from {info['console']}: ", "tertiary") + paint(info["url"], "accent"))
        import getpass
        try:
            key = getpass.getpass(f"Paste your {info['name']} key" + (f" (it starts with {info['prefix']}; "
                                  if info["prefix"] else " (") + "it isn't shown): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print(paint("Nothing was saved.", "tertiary"))
            return 130
    info = PROVIDERS[provider]
    if not key or not KEY_PATTERN.fullmatch(key):
        print(f"mobster login: that doesn't look like {'a' if provider != 'openai' else 'an'} {info['name']} key. "
              "Copy it again, without spaces.", file=sys.stderr)
        return 2
    variable = info["variable"]
    previous = os.environ.get(variable)
    os.environ[variable] = key
    if provider in ("anthropic", "openai"):
        previous_choice = os.environ.get("MOBSTER_SMART_PROVIDER")
        os.environ["MOBSTER_SMART_PROVIDER"] = provider
    result = {"ok": True, "message": ""}
    if not args.skip_check:
        if not args.json:
            print(paint("Checking your key…", "tertiary"), flush=True)
        try:
            result = _keys().test(provider)
        except Exception as error:  # noqa: BLE001 -- the test's own words, or a plain one
            result = {"ok": False, "message": f"Mobster couldn't check the key ({type(error).__name__})."}
    if not result.get("ok"):
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous
        if provider in ("anthropic", "openai"):
            if previous_choice is None:
                os.environ.pop("MOBSTER_SMART_PROVIDER", None)
            else:
                os.environ["MOBSTER_SMART_PROVIDER"] = previous_choice
        if args.json:
            print(json.dumps({"ok": False, "provider": provider, "message": result.get("message"),
                              "problem": result.get("problem")}))
        else:
            print(paint("✗ ", "coral", "bold") + (result.get("message") or "The key didn't work.") + " Nothing was saved.")
            link = result.get("link")
            if isinstance(link, dict) and link.get("url"):
                print("  " + paint(link["url"], "accent"))
        return 1
    changes = {provider: {"key": key}}
    if provider in ("anthropic", "openai"):
        changes["smartProvider"] = provider
    _keys().update(changes)
    from .config import SOURCES
    SOURCES[variable] = "mac-app"
    if args.json:
        print(json.dumps({"ok": True, "provider": provider, "key": mask(key), "checked": not args.skip_check}))
        return 0
    from .engines import smart_model
    model = f" · {smart_model()}" if provider != "jev" else ""
    print(paint("✓ ", "green", "bold") + paint(f"{info['name']} is connected", "bold") + f" · key {mask(key)}{model}")
    print("  " + paint("Saved on this Mac in Mobster's settings file (only you can read it). Mobster for Mac and "
                       "every `mobster` command use it.", "tertiary"))
    print("  Next: " + paint("mobster doctor", "accent") + " checks your iPhone, or " + paint("mobster", "accent")
          + " gives it a task.")
    return 0


def _guess(key):
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-"):
        return "openai"
    return None


def logout(args):
    """Remove one saved key from the settings file Mobster for Mac and `mobster login` share."""
    from .engines import model_provider, smart_model
    paint = _paint()
    saved = _saved()
    provider = args.provider
    if provider is None:
        provider = model_provider(smart_model()) if any(PROVIDERS[p]["variable"] in saved for p in
                                                        ("anthropic", "openai")) else None
        if provider is None:
            provider = "jev" if "TYPESAFE_API_KEY" in saved else None
    if provider is None:
        print("No key is saved for Mobster on this Mac.")
        return 0
    if PROVIDERS[provider]["variable"] not in saved:
        print(f"No {PROVIDERS[provider]['name']} key is saved for Mobster on this Mac.")
        if os.environ.get(PROVIDERS[provider]["variable"]):
            print("  " + paint(f"One is set in your shell or an env file ({PROVIDERS[provider]['variable']}); "
                               "remove it there.", "tertiary"))
        return 0
    changes = {provider: None}
    if saved.get("MOBSTER_SMART_PROVIDER") == provider:
        changes["smartProvider"] = None
    _keys().update(changes)
    print(paint("✓ ", "green", "bold") + f"Removed the {PROVIDERS[provider]['name']} key from this Mac.")
    return 0
