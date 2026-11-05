"""`mobster account ci-token`: the CI tokens a hosted CI runner signs in with (services/license-api/src/ci-tokens.ts).

    mobster account ci-token create NAME [--days N] [--json]   make a token; it is shown once
    mobster account ci-token list [--json]                     your tokens, never their secrets
    mobster account ci-token revoke ID [--json]                turn one off for good

The license API wants your account's access token. Mobster for Mac keeps that session to itself, so a terminal passes it
in MOBSTER_ACCOUNT_TOKEN; the console (Settings › CI tokens) does the same three things without it.

Exit codes: 0 done, 3 couldn't (usage too).
"""

import json
import os

from . import DevError, main, say, subcommands

TOKEN_ENV = "MOBSTER_ACCOUNT_TOKEN"
SIGN_IN = (f"Mobster can't read your account session from the terminal. Set {TOKEN_ENV} to your account's access "
           "token, or use Settings › CI tokens in the console.")


def add_arguments(parser, helpers):
    add = subcommands(parser, "account_command")
    group = add("ci-token", help="make, list and revoke the CI tokens a hosted runner signs in with",
                description="Manage CI tokens. A CI token lets a hosted CI runner run simulator checks with "
                            "`mobster ci run` and `mobster test`, with no Mac signed in. It never runs an iPhone, "
                            "adds no seat and counts no runs. It lasts 90 days unless you say otherwise, and you can "
                            "revoke it at any time. Needs " + TOKEN_ENV + ".",
                epilog="examples:\n  mobster account ci-token create \"app repo\"\n  mobster account ci-token list\n"
                       "  mobster account ci-token revoke <ID>")
    verbs = subcommands(group, "ci_token_command")
    create = verbs("create", help="make a CI token; it is shown once",
                   description="Make a CI token named NAME (the repository it is for). It prints the token once: "
                               "save it as the CI secret MOBSTER_CI_TOKEN.")
    create.add_argument("name", metavar="NAME", help="what it is for, such as the repository (up to 64 characters)")
    create.add_argument("--days", type=int, metavar="N", help="how long it lasts, 1 to 365 (default: 90)")
    listed = verbs("list", help="list your CI tokens", description="List your CI tokens and their state. A token's "
                                                                  "secret is never shown again.")
    revoke = verbs("revoke", help="turn a CI token off", description="Revoke a CI token. A runner with a network "
                                                                    "stops at its next run; one without stops when "
                                                                    "the token ends.")
    revoke.add_argument("id", metavar="ID", help="the token's id, from `mobster account ci-token list`")
    for sub in (create, listed, revoke):
        sub.add_argument("--json", action="store_true", help="print the answer as JSON")


def run(args, *, cwd=None, environ=None, call=None):
    environ = os.environ if environ is None else environ
    return main("account", {"ci-token": _ci_token}, args, environ=environ, call=call)


def _request(environ, call, method, path, body=None):
    from .. import share
    token = (environ.get(TOKEN_ENV) or "").strip()
    if not token:
        raise DevError(SIGN_IN)
    try:
        status, value = (call or share.call)(method, path, token, body)
    except share.ShareError as error:
        raise DevError(str(error)) from None
    if status >= 400:
        raise DevError(value.get("message") or f"Mobster's account service answered {status}.")
    return value


def _ci_token(args, environ, call):
    verb = args.ci_token_command
    if verb == "create":
        body = {"name": args.name, **({"days": args.days} if args.days is not None else {})}
        value = _request(environ, call, "POST", "/api/account/ci-tokens", body)
        if args.json:
            say(json.dumps(value))
        else:
            say(f"Created the CI token \"{value['name']}\", which works until {value['expires_at'][:10]}.\n"
                "It is shown once. Save it as the CI secret MOBSTER_CI_TOKEN:\n\n" + value["token"])
    elif verb == "list":
        value = _request(environ, call, "GET", "/api/account/ci-tokens")
        if args.json:
            say(json.dumps(value))
        elif not value.get("tokens"):
            say("You have no CI tokens. `mobster account ci-token create NAME` makes one.")
        else:
            for item in value["tokens"]:
                used = (item.get("last_used_at") or "never")[:10]
                say(f"{item['id']}  {item['state']:<8}  until {item['expires_at'][:10]}  used {used}  {item['name']}")
    else:
        value = _request(environ, call, "DELETE", f"/api/account/ci-tokens/{args.id}")
        say(json.dumps(value) if args.json else f"Revoked the CI token \"{value['token']['name']}\".")
    return 0
