"""A WKWebView's DOM as nodes the agent understands, and actions that target a selector.

WebDriverAgent has no web context: it reports a ``WebView`` element and its accessibility children, nothing more. The
DOM comes from the WebKit remote inspector, which a ``WebBridge`` wraps. The bridge is the only thing that talks to the
phone, so everything here runs on a fake. When no bridge is given, or it cannot be reached, the read falls back to the
accessibility tree and says why.

A DOM node's ``path`` is ``web:<selector>``. Like ``/pixels``, no Element locator starts with it, so a tap never lands
on another read's node. Page text is data: it fills labels and is never read as an instruction.
"""

import json
from dataclasses import dataclass

from ..verify.tree import AXNode

MAX_DOM_NODES = 1500
TEXT_LIMIT = 300
PATH_PREFIX = "web:"

# One call returns every visible, meaningful element: tag, role, text and frame in CSS pixels, plus a selector that
# finds it again (#id, [data-testid], else a tag:nth-of-type path).
DOM_SCRIPT = """(function () {
  var out = [], limit = %d;
  function sel(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    var t = el.getAttribute('data-testid');
    if (t) return '[data-testid=' + JSON.stringify(t) + ']';
    var parts = [];
    while (el && el.nodeType === 1 && el !== document.body) {
      var i = 1, s = el;
      while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      el = el.parentElement;
    }
    return 'body > ' + parts.join(' > ');
  }
  var all = document.body ? document.body.querySelectorAll('*') : [];
  for (var k = 0; k < all.length && out.length < limit; k++) {
    var el = all[k], r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var tag = el.tagName.toLowerCase(), role = el.getAttribute('role') || '';
    var text = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder || '').trim();
    var control = /^(a|button|input|select|textarea|summary)$/.test(tag) || role;
    if (!control && !(el.children.length === 0 && text)) continue;
    out.push({selector: sel(el), tag: tag, role: role, type: el.type || '', text: text.slice(0, %d),
              value: el.value || '', disabled: !!el.disabled,
              rect: [r.left, r.top, r.width, r.height]});
  }
  return {width: window.innerWidth, height: window.innerHeight, url: location.href, nodes: out};
})()""" % (MAX_DOM_NODES, TEXT_LIMIT)

ROLES = {"a": "Link", "button": "Button", "select": "PickerWheel", "textarea": "TextView", "summary": "Button",
         "h1": "StaticText", "h2": "StaticText", "h3": "StaticText", "img": "Image"}
ARIA_ROLES = {"button": "Button", "link": "Link", "textbox": "TextField", "searchbox": "SearchField",
              "checkbox": "Switch", "switch": "Switch", "tab": "Tab", "menuitem": "Button", "combobox": "PickerWheel"}
TOGGLE_INPUTS = {"checkbox", "radio"}
BUTTON_INPUTS = {"button", "submit", "reset"}


class WebUnreachable(RuntimeError):
    """No inspectable page: the app has no inspectable WebView, the inspector is off or the connection dropped."""


class WebActionError(RuntimeError):
    """An action on the page did not finish as asked. Its outcome is unknown: it is never re-sent."""


class WebBridge:
    """The WebKit remote inspector for one phone. A bridge subclasses this (none exists yet: docs/attach.md); tests
    pass a fake."""

    def contexts(self):
        """The inspectable pages: dicts with ``id``, ``bundle_id``, ``url`` and ``title``."""
        raise NotImplementedError

    def evaluate(self, context_id, script, timeout=10):
        """Run ``script`` in the page and return its JSON-decoded value; raise WebUnreachable if the page is gone."""
        raise NotImplementedError


@dataclass(frozen=True)
class WebPage:
    """One DOM read: ``nodes`` as AXNodes in document order, ``selectors`` mapping each node's path to its selector."""
    context_id: str
    url: str
    nodes: tuple
    selectors: dict


@dataclass(frozen=True)
class Attached:
    """What a read returned. ``source`` is ``web`` or ``ax``; ``reason`` says why a read fell back to ``ax``."""
    source: str
    nodes: tuple
    page: WebPage | None = None
    reason: str = ""


def _string(value, limit=TEXT_LIMIT):
    return value[:limit] if isinstance(value, str) else ""


def _number(value):
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and abs(value) < 1e7 else 0.0


def role_for(item):
    aria = ARIA_ROLES.get(_string(item.get("role"), 40))
    tag, kind = _string(item.get("tag"), 20), _string(item.get("type"), 20)
    if aria:
        return aria
    if tag == "input":
        if kind in TOGGLE_INPUTS:
            return "Switch"
        if kind in BUTTON_INPUTS:
            return "Button"
        return "SearchField" if kind == "search" else "TextField"
    return ROLES.get(tag, "StaticText")


def parse_dom(data, frame):
    """The DOM script's value as ``(url, nodes, selectors)``. ``frame`` is the WebView's (x, y, w, h) in points.

    CSS pixels scale to points by the WebView's width over the page's viewport width. Anything malformed is dropped.
    """
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise WebUnreachable("the page returned no DOM")
    fx, fy, fw, _ = frame
    viewport = _number(data.get("width"))
    scale = fw / viewport if viewport > 0 and fw > 0 else 1.0
    nodes, selectors = [], {}
    for item in data["nodes"][:MAX_DOM_NODES]:
        selector = _string(item.get("selector"), 500) if isinstance(item, dict) else ""
        rect = item.get("rect") if selector else None
        if not isinstance(rect, list) or len(rect) != 4:
            continue
        x, y, w, h = (_number(v) * scale for v in rect)
        path = PATH_PREFIX + selector
        if path in selectors:
            continue
        identifier = selector[1:] if selector.startswith("#") else ""
        nodes.append(AXNode(path=path, role=role_for(item), identifier=identifier, label=_string(item.get("text")),
                            value=_string(item.get("value")), placeholder="", rect=(fx + x, fy + y, w, h),
                            enabled=not item.get("disabled"), selected=False, visible=True))
        selectors[path] = selector
    return _string(data.get("url"), 500), tuple(nodes), selectors


def web_view(tree):
    """The largest shown WebView in the accessibility tree, or None."""
    views = [node for node in tree.shown() if node.role == "WebView"]
    return max(views, key=lambda node: node.rect[2] * node.rect[3], default=None)


def pick_context(contexts, bundle_id):
    """The app's one inspectable page. The listing doesn't say which page is on screen (Safari lists every tab, in
    the order they opened, measured 26 Oct 2026), so an app with several is refused rather than read at a guess."""
    mine = [c for c in contexts if isinstance(c, dict) and c.get("bundle_id") == bundle_id and c.get("id")]
    if len(mine) > 1:
        raise WebUnreachable(f"the app has {len(mine)} inspectable pages and the inspector doesn't say which is on "
                             "screen")
    return mine[0] if mine else None


def read(tree, bridge=None):
    """The agent's nodes: the DOM when the screen holds a reachable WebView, else the accessibility tree."""
    view = web_view(tree)
    if view is None:
        return Attached("ax", tree.shown(), reason="no WebView on screen")
    if bridge is None:
        return Attached("ax", tree.shown(), reason="no web inspector bridge")
    try:
        context = pick_context(bridge.contexts(), tree.bundle_id)
        if context is None:
            raise WebUnreachable("the app's page is not inspectable")
        url, nodes, selectors = parse_dom(bridge.evaluate(context["id"], DOM_SCRIPT), view.rect)
    except WebUnreachable as error:
        return Attached("ax", tree.shown(), reason=str(error))
    page = WebPage(context["id"], url, nodes, selectors)
    outside = [node for node in tree.shown() if not inside(node, view)]
    return Attached("web", tuple(outside) + nodes, page=page)


def inside(node, view):
    x, y, w, h = node.rect
    vx, vy, vw, vh = view.rect
    return node is view or (x >= vx and y >= vy and x + w <= vx + vw + 1 and y + h <= vy + vh + 1)


def _act(bridge, page, path, body):
    selector = page.selectors.get(path)
    if selector is None:
        raise WebActionError("that element is not in the page that was read")
    script = "(function (s) { var el = document.querySelector(s); if (!el) return 'missing'; %s; return 'ok'; })(%s)"
    script %= (body, json.dumps(selector))
    try:
        result = bridge.evaluate(page.context_id, script)
    except WebUnreachable as error:
        raise WebActionError(f"the page went away before the action finished ({error}); check the screen") from None
    if result != "ok":
        raise WebActionError("the element is no longer on the page; read the screen again")


def click(bridge, page, path):
    """Click the element read at ``path``. One attempt: an error means the outcome is unknown."""
    _act(bridge, page, path, "el.scrollIntoView({block: 'center'}); el.click()")


def type_text(bridge, page, path, text):
    """Set a field's value and fire ``input`` and ``change``. One attempt, like ``click``."""
    body = ("el.focus(); el.value = %s; el.dispatchEvent(new Event('input', {bubbles: true})); "
            "el.dispatchEvent(new Event('change', {bubbles: true}))" % json.dumps(text))
    _act(bridge, page, path, body)
