"""A Google Sheets destination: each result is a row in "Mobster results",
a sheet Mobster makes in your Google Drive."""

import json
import re
from urllib.parse import quote, urlencode

from . import connect, net
from .kinds import Kind, again, careful, connection, flat
from .webhook import USER_AGENT, Outcome

SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3/files"
TITLE = "Mobster results"
LABEL = "Google Sheets"
SHEET_ID = re.compile(r"[A-Za-z0-9_-]{20,200}")
CELL_MAX = 50_000       # Google's limit for one cell's text


def headers(token, body=False):
    out = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    if body:
        out["Content-Type"] = "application/json"
    return out


def refusal(status):
    """Google's refusal as a sentence: what happened, then what to do."""
    if status == 401:
        return f"{LABEL} signed Mobster out. " + again(LABEL)
    if status in (403, 404):
        return (f"Mobster can't open {TITLE} any more: it was deleted, or Google took back Mobster's access. "
                f"Connect {LABEL} again to make a new one.")
    return f"Google answered {status}."


def find_or_make(call, token):
    """{"id", "title"}: the "Mobster results" sheet Mobster made before, else a new one. NetError, or ValueError."""
    query = f"name = '{TITLE}' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
    reply = call("GET", f"{DRIVE}?" + urlencode({"q": query, "fields": "files(id,name)", "orderBy": "createdTime",
                                                  "pageSize": "1", "spaces": "drive"}), None, headers(token))
    answer = reply.json_object()
    if reply.status != 200 or answer is None:
        raise ValueError(refusal(reply.status))
    files = [item for item in answer.get("files") or () if isinstance(item, dict)
             and isinstance(item.get("id"), str) and SHEET_ID.fullmatch(item["id"])]
    if files:
        return {"id": files[0]["id"], "title": TITLE}
    sheet = {"properties": {"title": TITLE},
             "sheets": [{"properties": {"title": "Results", "gridProperties": {"frozenRowCount": 1}}}]}
    reply = call("POST", SHEETS, json.dumps(sheet).encode(), headers(token, body=True))
    answer = reply.json_object()
    made = (answer or {}).get("spreadsheetId")
    if reply.status != 200 or not isinstance(made, str) or not SHEET_ID.fullmatch(made):
        raise ValueError(f"Google didn't make the sheet ({reply.status}). Connect again.")
    return {"id": made, "title": TITLE}


def value(item):
    """A cell: numbers and true/false as they are, anything else as text Google keeps as written (RAW)."""
    if item is None:
        return ""
    if isinstance(item, bool) or (isinstance(item, (int, float)) and item == item and abs(item) != float("inf")):
        return item
    text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
    return text[:CELL_MAX]


class Stop(Exception):
    """One try ends here, with ``outcome``."""

    def __init__(self, outcome):
        super().__init__(outcome.message)
        self.outcome = outcome


class Sheets(Kind):
    id, label = "sheets", LABEL
    example = "Sales sheet"
    fields = ("connect",)
    replay_safe = False
    provider = "google-sheets"

    def connected(self, call, tokens):
        sheet = find_or_make(call, tokens["access_token"])
        oauth = {key: item for key, item in tokens.items() if key != "kept"}
        return {"credentials": {"oauth": oauth, "sheet": sheet}, "account": sheet["title"], "targets": None}

    def setup(self, body, current=None):
        if current is not None and "connect" not in body:
            return {}
        found = connection(body, self)["credentials"]
        return {"hint": f"{LABEL} › {found['sheet']['title']}", "has_secret": True,
                "config": {"spreadsheet": found["sheet"]["id"], "title": found["sheet"]["title"]},
                "vault": {"url": None, "secret": None, "oauth": found["oauth"]}, "last_test": None}

    @staticmethod
    def _sheet(ctx):
        sheet = ctx.destination["config"].get("spreadsheet")
        if not sheet:
            # A sheet saved before 15 Oct 2026 took rows through an Apps Script web app.
            raise Stop(Outcome(False, False, None, "Mobster now adds rows itself, with no script. " + again(LABEL),
                               unsent=True))
        return f"{SHEETS}/{quote(sheet, safe='')}"

    @staticmethod
    def _call(ctx, method, url, data, token):
        """One request that adds nothing (a read, or the header, written the same each time it's tried), with a token
        that works: a 401 renews it once."""
        for refresh in (False, True):
            if refresh:
                token = connect.access(ctx, LABEL, refresh=True)
            try:
                reply = ctx.call(method, url, data, headers(token, body=data is not None))
            except net.NetError as error:
                # Nothing this sends can add a row, so another try is safe even when this one may have arrived.
                raise Stop(Outcome(False, error.retry, None, error.message, unsent=True)) from None
            if reply.status == 401 and not refresh:
                continue
            if reply.status == 200:
                return reply, token
            raise Stop(Outcome(False, reply.status == 429 or reply.status >= 500, reply.status,
                               refusal(reply.status), unsent=True))
        raise Stop(Outcome(False, False, 401, refusal(401), unsent=True))

    def _header(self, ctx, base, token):
        reply, token = self._call(ctx, "GET", f"{base}/values/1:1?majorDimension=ROWS", None, token)
        rows = (reply.json_object() or {}).get("values") or [[]]
        return [str(name) for name in rows[0]], token

    def send(self, ctx, body):
        try:
            base = self._sheet(ctx)
            token = connect.access(ctx, LABEL)
            header, token = self._header(ctx, base, token)
            row = flat(body)
            known = {name.casefold() for name in header}
            missing = [name for name in row if name.casefold() not in known]
            if missing:
                header += missing
                data = json.dumps({"values": [header]}, ensure_ascii=False).encode()
                _reply, token = self._call(ctx, "PUT", f"{base}/values/A1?valueInputOption=RAW", data, token)
        except (Stop, connect.NoToken) as error:
            return error.outcome
        by_name = {name.casefold(): item for name, item in row.items()}
        data = json.dumps({"values": [[value(by_name.get(name.casefold())) for name in header]]},
                          ensure_ascii=False).encode()
        # RAW input, so text from a screen is never read as a formula.
        url = f"{base}/values/A1:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
        reply, failed = careful(lambda: ctx.call("POST", url, data, headers(token, body=True)), TITLE)
        if failed:
            return failed
        if reply.status == 200:
            return Outcome(True, False, 200)
        if reply.status == 429:
            return Outcome(False, True, 429, "Google asked Mobster to wait.", unsent=True)
        if reply.status >= 500:
            return Outcome(False, False, reply.status, f"Google answered {reply.status}. Check {TITLE}, then resend "
                                                       "if the row is missing.")
        return Outcome(False, False, reply.status, refusal(reply.status), unsent=True)

    def test(self, ctx):
        """Reads the header: the sign-in and the sheet work. No row is added."""
        try:
            self._header(ctx, self._sheet(ctx), connect.access(ctx, LABEL))
        except (Stop, connect.NoToken) as error:
            return Outcome(False, False, error.outcome.status, error.outcome.message)
        title = ctx.destination["config"].get("title") or TITLE
        return Outcome(True, False, 200, f"Connected to {title}. Each result adds a row.")
