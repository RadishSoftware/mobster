"""`mobster login`, `mobster login status` and `mobster logout`: your model key, saved for Mobster on this Mac."""

import json
import os
import sys

PROVIDERS = {
    "anthropic": {"name": "Claude", "company": "Anthropic", "variable": "ANTHROPIC_API_KEY", "prefix": "sk-ant-",
                  "console": "the Claude Console", "url": "https://platform.claude.com/settings/keys"},
    "openai": {"name": "OpenAI", "company": "OpenAI", "variable": "OPENAI_API_KEY", "prefix": "sk-",
               "console": "the OpenAI Platform", "url": "https://platform.openai.com/api-keys"},
}
ORDER = ("anthropic", "openai")


def mask(key):
    """sk-ant-…a1F2: the key's well-known prefix and its last four characters, never more."""
    key = (key or "").strip()
    if len(key) < 12:
        return "…" + key[-2:] if key else ""
    for prefix in ("sk-ant-", "sk-proj-", "sk-svcacct-", "sk-"):
        if key.startswith(prefix):
            return f"{prefix}…{key[-4:]}"
    return f"…{key[-4:]}"


def source_words(variable):
    """Where a saved key came from, in words: Mobster for Mac's settings, an env file, or your shell."""
    from .config import SOURCES
    source = SOURCES.get(variable)
    if source == "mac-app":
        return "from Mobster for Mac"
    if source == "env-file":
        return "from your env file"
    return "from your shell"


# The CLI agents sign in with their own CLI: Mobster only runs them (ai_plans/agents.py).
CLI_SIGN_IN = {"codex": ("Codex", "codex login"),
               "claude_code": ("Claude Code", "claude, then /login"), "claude": ("Claude Code", "claude, then /login"),
               "gemini": ("Gemini CLI", "gemini, then choose Sign in with Google")}


def run(args):
    action = getattr(args, "action", None)
    if args.command == "logout":
        account = getattr(args, "account", None)
        if account:
            return logout_provider(account)
        return logout(args)
    if action == "status":
        return status(args)
    if action in CLI_SIGN_IN:
        name, command = CLI_SIGN_IN[action]
        print(f"{name} signs in itself: in Terminal, run {command}. Mobster runs your {name} from then on; it never "
              "sees your sign-in.")
        return 0
    if action:
        return login_provider(args, action)
    return login(args)


def login_provider(args, provider_id):
    """`mobster login <plan or provider>`."""
    import sys

    from .ai_plans import device_login, providers, routes
    paint = _paint()
    provider = providers.get(provider_id)
    if provider is None:
        known = ", ".join(sorted(["codex", "claude", "gemini", *providers.ALL]))
        print(f"mobster login: no plan or provider called {provider_id}. Try one of: {known}", file=sys.stderr)
        return 2
    body = {}
    if getattr(args, "model", None):
        body["model"] = args.model
    if getattr(args, "base_url", None):
        body["baseUrl"] = args.base_url
    try:
        if provider.mechanism == "device":
            attempt = device_login.start(provider.id)
            print(paint(f"Sign in to {provider.name}", "bold") + ": open " + paint(attempt["url"], "accent")
                  + " and enter " + paint(attempt["user_code"], "bold") + ".")
            print("  " + paint("Waiting for you to finish. ctrl+c cancels.", "tertiary"), flush=True)
            device_login.wait(provider.id, attempt)
            if body:
                routes.handle(_keys(), provider.id, "key", body)
        else:
            if provider.mechanism in ("key", "custom"):
                if getattr(args, "with_key", False):
                    key = sys.stdin.read().strip()
                else:
                    import getpass
                    where = f" (from {provider.console})" if provider.console else ""
                    key = getpass.getpass(f"Paste your {provider.name} key{where}: ").strip()
                body["key"] = key
            routes.handle(_keys(), provider.id, "key", body)
        routes.handle(_keys(), provider.id, "use", {})
    except KeyboardInterrupt:
        print()
        print(paint("Cancelled. Nothing was saved.", "tertiary"))
        return 130
    except (routes.RouteError, device_login.DeviceError) as error:
        print(paint("✗ ", "coral", "bold") + str(error))
        return 1
    model = providers.chosen_model(provider)
    vision = "" if provider.vision(model) else " It can't see images, so it reads the screen as text."
    bill = f"on {provider.plan_label}" if provider.group == "plan" else f"on your {provider.name} key"
    print(paint("✓ ", "green", "bold") + f"Mobster's agent runs on {model}, {bill}.{vision}")
    return 0


def logout_provider(provider_id):
    import sys

    from .ai_plans import providers, routes
    if providers.get(provider_id) is None:
        print(f"mobster logout: no plan or provider called {provider_id}.", file=sys.stderr)
        return 2
    routes.handle(_keys(), provider_id, "forget", {})
    print(f"Mobster forgot {providers.get(provider_id).name} on this Mac.")
    return 0


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
    wanted = {p["variable"] for p in PROVIDERS.values()} | {"MOBSTER_SMART_PROVIDER"}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() in wanted:
                values[name.strip()] = value.strip().strip("'\"")
    except (OSError, UnicodeDecodeError):
        pass
    # The keys themselves are in the Keychain once the Mac app or this command saved them there (env_secrets.py).
    from . import env_secrets
    values.update({name: value for name, value in env_secrets.load(path).items() if name in wanted})
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
    from .ai_plans import providers
    ready = [row for row in providers.public() if row["configured"]]
    smart = model_provider(smart_model()) if any(r["set"] for r in rows) or ready else None
    if getattr(args, "json", False):
        print(json.dumps({"keys": rows, "agent": smart, "model": smart_model() if smart else None,
                          "providers": [{key: row[key] for key in ("id", "name", "model", "vision")}
                                        for row in ready]}))
        return 0
    print(paint("Keys", "bold"))
    for row in rows:
        name = paint(row["name"].ljust(8), "accent")
        if row["set"]:
            note = "  Mobster's agent uses it" if row["provider"] == smart else ""
            print(f"  {paint('✓', 'green')} {name}{row['key'].ljust(16)}{paint(row['source'], 'tertiary')}"
                  + paint(note, "secondary"))
        else:
            print(f"  {paint('–', 'tertiary')} {name}{paint('not set', 'tertiary')}")
    if ready:
        print(paint("Plans and providers", "bold"))
        for row in ready:
            uses = paint("  Mobster's agent uses it", "secondary") if smart == row["id"] else ""
            vision = "" if row["vision"] else paint("  reads the screen as text", "tertiary")
            print(f"  {paint('✓', 'green')} {row['name']}  {row['model']}{vision}{uses}")
    if not any(row["set"] for row in rows) and not ready:
        print("\nNo key yet. Run " + paint("mobster login", "accent") + ", or pick Codex or Claude Code in the Mac "
              "app's composer to run on your plan.")
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


def save_key(provider, key, *, check=True):
    """Check a pasted key and save it, as `mobster login` and the terminal UI both do."""
    from .env_secrets import REFUSED, KeychainError
    from .keys import KEY_PATTERN
    info = PROVIDERS[provider]
    if not key or not KEY_PATTERN.fullmatch(key):
        return {"ok": False, "problem": "pattern", "message":
                f"That doesn't look like {'an' if provider == 'openai' else 'a'} {info['name']} key. "
                "Copy it again, without spaces."}
    variable = info["variable"]
    saved = {name: os.environ.get(name) for name in (variable, "MOBSTER_SMART_PROVIDER")}
    os.environ[variable], os.environ["MOBSTER_SMART_PROVIDER"] = key, provider

    def fail(result):
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return {"ok": False, **result}

    keys = _keys()
    if check:
        try:
            result = keys.test(provider)
        except Exception as error:  # noqa: BLE001 -- the test's own words, or a plain one
            result = {"ok": False, "message": f"Mobster couldn't check the key ({type(error).__name__})."}
        if not result.get("ok"):
            return fail({"problem": result.get("problem"), "link": result.get("link"),
                         "message": (result.get("message") or "The key didn't work.") + " Nothing was saved."})
    try:
        keys.update({provider: {"key": key}, "smartProvider": provider})
    except KeychainError:
        return fail({"problem": "keychain", "message": REFUSED})
    from .config import SOURCES
    SOURCES[variable] = "mac-app"
    return {"ok": True, "problem": None, "message": f"{info['name']} is connected."}


def login(args):
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
    if not args.skip_check and not args.json:
        print(paint("Checking your key…", "tertiary"), flush=True)
    result = save_key(provider, key, check=not args.skip_check)
    if result["problem"] == "pattern":
        print(f"mobster login: {result['message']}", file=sys.stderr)
        return 2
    if not result["ok"]:
        if args.json:
            print(json.dumps({"ok": False, "provider": provider, "message": result["message"],
                              "problem": result["problem"]}))
        else:
            print(paint("✗ ", "coral", "bold") + result["message"])
            link = result.get("link")
            if isinstance(link, dict) and link.get("url"):
                print("  " + paint(link["url"], "accent"))
        return 1
    if args.json:
        print(json.dumps({"ok": True, "provider": provider, "key": mask(key), "checked": not args.skip_check}))
        return 0
    from .engines import smart_model
    print(paint("✓ ", "green", "bold") + paint(f"{info['name']} is connected", "bold") + f", key {mask(key)}, "
          f"{smart_model()}")
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
        known = [name for name in ORDER if PROVIDERS[name]["variable"] in saved]
        chosen = model_provider(smart_model())
        # A plan or another provider can be selected for Smart: it names no key here, so only a sole key is removed.
        provider = chosen if chosen in known else known[0] if len(known) == 1 else None
        if provider is None and known:
            print("mobster logout: more than one key is saved. Choose one with --provider "
                  + ", ".join(known) + ".", file=sys.stderr)
            return 2
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
