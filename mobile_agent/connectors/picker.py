"""Google's Picker, where you choose the Google Drive files Mobster may open."""

import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..api_errors import APIError
from . import callback_page, shared
from . import client as mcp
from . import presets as preset_list

WAIT_SECONDS = 300.0
NAME = "Google Drive"
LOADER = "https://apis.google.com/js/api.js"
PROJECT = re.compile(r"\d{6,20}")      # the Google Cloud project number a client ID starts with
NOT_FOUND = re.compile(r"\b40[34]\b|not[\s_-]*found|permission|forbidden|insufficient", re.I)
UNPICKED = ("Mobster can open only the Google Drive files the person picked. Tell them to pick this file with Choose "
            "files on Google Drive's page in Settings › Connectors, then ask again.")
NO_KEY = "Mobster's Google Drive file picker isn't set up yet, so Choose files can't open. Try again later."
OWN_APP = ("Choose files works with Mobster's own Google app, and this Google Drive signs in with yours. Remove Google "
           "Drive, then connect it again without your own app.")
COPY = {
    "pick": ["Choose files for Mobster", "Mobster opens only the Google Drive files you pick "
             "here, and the files it made."],
    "one": "Mobster can open 1 file",
    "many": "Mobster can open {n} files",
    "none": ["No files picked", "Mobster opens only the files you pick. Choose files to pick some."],
    "error": ["Google’s file picker didn’t open", "Check your internet connection, then "
              "choose files again in Mobster."],
    "back": callback_page.BACK,
}
STYLE = """
.actions { display: flex; justify-content: center; gap: 8px; margin-top: 24px; }
.actions a { margin-top: 0; }
button { height: 32px; padding: 0 16px; border: 0; border-radius: 999px; background: var(--ink); color: var(--paper);
  font: inherit; font-weight: 500; letter-spacing: -.011em; cursor: pointer;
  transition: background-color 150ms var(--ease-standard); }
button:hover { background: var(--primary-hover); }
button:focus-visible { outline: 2px solid var(--violet-solid); outline-offset: 2px; }
button[hidden] { display: none; }
"""
# The page's script: it loads Google's loader, then the Picker, and opens it; Choose files opens it again. What you
# pick stays between you and Google: the page only says how many files Mobster can now open, then hands back to Mobster.
SCRIPT = """(function () {
  var config = %s;
  var main = document.querySelector("main"), title = document.querySelector("h1"), line = document.querySelector("main > p");
  var knot = document.querySelector(".knot svg"), choose = document.getElementById("choose"), picker = null;
  function show(state, words) {
    main.setAttribute("data-state", state);
    knot.innerHTML = config.glyphs[state] || "";
    title.textContent = document.title = words[0];
    line.textContent = words[1];
  }
  function failed() { show("error", config.copy.error); choose.hidden = true; }
  function picked(data) {
    var action = data[google.picker.Response.ACTION];
    if (action === google.picker.Action.PICKED) {
      var count = (data[google.picker.Response.DOCUMENTS] || []).length;
      show("ok", [count === 1 ? config.copy.one : config.copy.many.replace("{n}", count), config.copy.back]);
      choose.hidden = true;
      setTimeout(function () { location.href = config.app; }, matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 900);
    } else if (action === google.picker.Action.CANCEL) {
      show("denied", config.copy.none);
    }
  }
  function open() {
    if (!window.google || !google.picker) return failed();
    if (!picker) {
      var view = new google.picker.DocsView(google.picker.ViewId.DOCS).setIncludeFolders(true).setSelectFolderEnabled(false);
      picker = new google.picker.PickerBuilder().setAppId(config.appId).setOAuthToken(config.token)
        .setDeveloperKey(config.key).addView(view).enableFeature(google.picker.Feature.MULTISELECT_ENABLED)
        .enableFeature(google.picker.Feature.SUPPORT_DRIVES).setTitle(config.copy.pick[0]).setCallback(picked).build();
    }
    show("pick", config.copy.pick);
    picker.setVisible(true);
  }
  choose.addEventListener("click", open);
  var loader = document.createElement("script");
  loader.src = config.loader;
  loader.onerror = failed;
  loader.onload = function () { gapi.load("picker", { callback: open, onerror: failed, timeout: 15000, ontimeout: failed }); };
  document.head.appendChild(loader);
})();"""  # noqa: E501


def csp(nonce):
    """The page's Content-Security-Policy: its own script by ``nonce`` and what that script loads (Google's loader and
    the Picker, 'strict-dynamic'), the Picker's frame and styles from Google, and nothing else."""
    return (f"default-src 'none'; script-src 'nonce-{nonce}' 'strict-dynamic'; "
            "style-src 'unsafe-inline' https://*.google.com https://*.gstatic.com; "
            "img-src data: https://*.google.com https://*.gstatic.com https://*.googleusercontent.com; "
            "font-src data: https://fonts.gstatic.com; "
            "frame-src https://docs.google.com https://drive.google.com https://accounts.google.com "
            "https://content.googleapis.com; connect-src https://*.google.com https://*.googleapis.com; base-uri "
            "'none'; form-action 'none'; frame-ancestors 'none'")


class Pick:
    """One page of Google's Picker on 127.0.0.1, handed out once, within WAIT_SECONDS. ``begin()`` -> its address. The
    token goes from the Keychain to this page and on to Google, never to a Mobster server; Google checks the key against
    the page's origin."""

    def __init__(self, token, key, app_id, wait=WAIT_SECONDS):
        self.token, self.key, self.app_id, self.wait = token, key, app_id, wait
        self.path = "/pick/" + secrets.token_urlsafe(24)
        self.server, self.port, self.served = None, None, False
        self._done = threading.Event()
        self._once = threading.Lock()

    def begin(self):
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        except OSError:
            raise APIError("Mobster couldn't open Google Drive's file picker on this Mac. Try again.", 503,
                           "picker_unavailable") from None
        self.port = self.server.server_address[1]
        threading.Thread(target=self._serve, name="mobster-drive-picker", daemon=True).start()
        return f"http://127.0.0.1:{self.port}{self.path}"

    def _serve(self):
        self.server.timeout = 0.5
        deadline = time.monotonic() + self.wait
        while not self._done.is_set() and time.monotonic() < deadline:
            self.server.handle_request()
        self.server.server_close()
        self.token = None

    def cancel(self):
        self._done.set()

    def page(self, nonce):
        config = {"token": self.token, "key": self.key, "appId": self.app_id, "loader": LOADER, "copy": COPY,
                  "glyphs": callback_page.GLYPHS, "app": callback_page.APP_LINK}
        # As data inside the script: nothing in it can end the script element.
        script = SCRIPT % json.dumps(config).replace("<", "\\u003c")
        return callback_page.document("pick", NAME, *COPY["pick"], script=script, nonce=nonce, style=STYLE,
                                      actions='<button type="button" id="choose">Choose files</button>',
                                      referrer="strict-origin")

    def _handler(self):
        pick = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, status, body=b"", headers=()):
                self.send_response(status)
                for key, value in (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), *headers):
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                # The page's own path, asked for by this address (not a name another site points here).
                if self.path != pick.path or self.headers.get("Host") != f"127.0.0.1:{pick.port}":
                    return self._send(404)
                with pick._once:
                    served, pick.served = pick.served, True
                if served or not pick.token:
                    body = callback_page.document("error", NAME, "This page was opened already",
                                                  "Choose files again in Mobster.").encode()
                    return self._send(410, body, (("Content-Type", "text/html; charset=utf-8"),
                                                  ("Content-Security-Policy", callback_page.csp())))
                nonce = secrets.token_urlsafe(18)
                body = pick.page(nonce).encode()
                pick.cancel()   # handed out once: the listener stops after this answer
                self._send(200, body, (("Content-Type", "text/html; charset=utf-8"),
                                       ("Content-Security-Policy", csp(nonce)), ("Referrer-Policy", "strict-origin")))

        return Handler


_picks = {}        # connector id -> its open Pick: a new one closes the one before
_lock = threading.Lock()


def begin(service, connector_id):
    """{"pickUrl"}: Google's Picker for the Google Drive connector ``connector_id``, on a page of this Mac."""
    row = service._get(connector_id)
    preset = preset_list.preset(row["preset"]) if row.get("preset") else None
    if not (preset or {}).get("picker"):
        raise ValueError(f"{row['name']} has no files to choose. Choose files is for Google Drive")
    app = service.apps.get(preset["key"])
    if isinstance(app, shared.Unavailable):
        raise APIError(f"{shared.why(preset['name'], app.reason)}, so Choose files can't open. Try again later.", 503,
                       app.reason)
    project = app.client_id.split("-", 1)[0]
    if not app.picker_key or not PROJECT.fullmatch(project):
        raise APIError(NO_KEY, 503, "picker_unconfigured")
    try:
        token = service.bearer(connector_id)
        client_id = (service._secrets(row).get("oauth") or {}).get("client_id")
    except mcp.AuthNeeded:
        raise APIError(f"Connect {row['name']} first, then choose files.", 409, "sign_in") from None
    except mcp.NotSent as error:
        raise APIError(error.message, 503, "keychain_unavailable") from None
    if client_id != app.client_id:
        raise APIError(OWN_APP, 409, "own_app")
    pick = Pick(token, app.picker_key, project)
    url = pick.begin()
    with _lock:
        previous, _picks[connector_id] = _picks.get(connector_id), pick
    if previous is not None:
        previous.cancel()
    return {"pickUrl": url}


def after_sign_in(service, connector_id):
    """oauth.SignIn's ``then`` for Google Drive: the Picker's page once signed in, or None (the done page) when it can't
    open."""
    def then(_tokens):
        try:
            return begin(service, connector_id)["pickUrl"]
        except (APIError, ValueError):
            return None
    return then


def unpicked(row, text):
    """UNPICKED when ``row``'s service shows Mobster only what you pick and ``text``, its answer to a call, says not
    found or not allowed; else None."""
    preset = preset_list.preset(row.get("preset")) if row.get("preset") else None
    return UNPICKED if (preset or {}).get("picker") and NOT_FOUND.search(text or "") else None
