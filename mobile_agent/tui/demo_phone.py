"""A scripted phone for `mobster --demo`: the real agent loop, no iPhone and no model calls."""

import base64
import io
import re
import threading
import time
from functools import lru_cache

from ..drivers import Driver
from ..state import Element, Snapshot

SOURCE = "synthetic_fixture"
MODEL = "scripted-demo"
DEFAULT_MESSAGE = "Running 10 minutes late, save me a seat"

MESSAGES, SETTINGS, WALLET = "com.apple.MobileSMS", "com.apple.Preferences", "com.apple.Passbook"


def _element(index, label, role, rect, **extra):
    return Element(str(index), label, role, rect, **extra)


def _message(goal):
    """The text to send: a quoted part of the request, else a stock line."""
    match = re.search(r"[\"“']([^\"”']{2,120})[\"”']", goal or "")
    return match.group(1) if match else DEFAULT_MESSAGE


def _query(goal):
    match = re.search(r"(?:search(?: for)?|find|look up)\s+(.+?)(?:\s+(?:in|on)\s+\w+)?$", goal or "", re.I)
    return (match.group(1).strip(" .\"“”'") if match else "coffee")[:60] or "coffee"


class Screens:
    """The screens of one scenario, keyed by stage name: (elements, text)."""

    ROWS = ["Apple Account", "Airplane Mode", "Wi-Fi", "Bluetooth", "Cellular", "Battery", "General",
            "Accessibility"]
    MORE_ROWS = ["General", "Accessibility", "Camera", "Control Center", "Display & Brightness",
                 "Home Screen & App Library", "Search", "Siri", "StandBy", "Wallpaper"]
    THREADS = [("Alex Rivera", "See you at 7? I'll grab a table"), ("Mom", "Call me when you land"),
               ("Jordan Lee", "Sent the deck, take a look"), ("Priya Shah", "Haha yes"),
               ("Dentist", "Reminder: cleaning Tue 9:30")]

    def __init__(self, bundle, goal):
        self.bundle, self.goal = bundle, goal
        self.message = _message(goal)
        self.query = _query(goal)

    def build(self, stage, typed=""):
        if self.bundle == MESSAGES:
            return self.messages(stage, typed)
        if self.bundle == SETTINGS:
            return self.settings(stage)
        if self.bundle == WALLET:
            elements = [_element(0, "Add", "Button", (.84, .065, .12, .035)),
                        _element(1, "Apple Card", "Button", (.05, .14, .9, .26)),
                        _element(2, "Transit", "Button", (.05, .43, .9, .26))]
            return elements, "Wallet\nApple Card\nTransit\nAdd"
        return self.search(stage, typed)

    def messages(self, stage, typed):
        if stage == "inbox":
            elements = [_element(90, "Messages", "NavigationBar", (0, .05, 1, .08)),
                        _element(0, "Edit", "Button", (.04, .065, .12, .035)),
                        _element(1, "Compose", "Button", (.86, .065, .1, .035)),
                        _element(2, "Search", "SearchField", (.04, .15, .92, .045), editable=True,
                                 actions=("TAP", "TYPE"))]
            for row, (name, preview) in enumerate(self.THREADS):
                elements.append(_element(3 + row, name, "Cell", (0, .22 + row * .1, 1, .095)))
            text = "Messages\n" + "\n".join(f"{name}\n{preview}" for name, preview in self.THREADS)
            return elements, text
        sent = stage == "sent"
        value = typed if stage == "typed" else ""
        elements = [_element(90, "Alex Rivera", "NavigationBar", (0, .05, 1, .08)),
                    _element(0, "Messages", "Button", (.02, .065, .24, .035)),
                    _element(1, "Alex Rivera", "StaticText", (.3, .06, .4, .04)),
                    _element(2, "See you at 7? I'll grab a table", "StaticText", (.05, .2, .7, .06)),
                    _element(3, "Message", "TextField", (.12, .9, .7, .045), editable=True, value=value,
                             actions=("TAP", "TYPE"))]
        if stage == "typed":
            elements.append(_element(4, "Send", "Button", (.85, .9, .1, .045)))
        if sent:
            elements.append(_element(5, self.message, "StaticText", (.3, .3, .65, .06)))
            elements.append(_element(6, "Delivered", "StaticText", (.72, .37, .23, .025)))
        text = "Alex Rivera\nSee you at 7? I'll grab a table" + (f"\n{self.message}\nDelivered" if sent else "")
        return elements, text

    def settings(self, stage):
        if stage in {"top", "scrolled"}:
            rows = self.ROWS if stage == "top" else self.MORE_ROWS
            elements = [_element(90, "Settings", "NavigationBar", (0, .05, 1, .07)),
                        _element(0, "Search", "SearchField", (.04, .13, .92, .045), editable=True,
                                 actions=("TAP", "TYPE"))]
            for row, name in enumerate(rows):
                elements.append(_element(1 + row, name, "Cell", (0, .2 + row * .075, 1, .07)))
            return elements, "Settings\n" + "\n".join(rows)
        dark = stage == "dark"
        elements = [_element(90, "Display & Brightness", "NavigationBar", (0, .05, 1, .07)),
                    _element(0, "Settings", "Button", (.02, .065, .22, .035)),
                    _element(1, "Display & Brightness", "StaticText", (.25, .06, .5, .04)),
                    # The two appearances read as a pair of toggles, so the finished task can show its proof:
                    # "Dark: On", the switch the request named at the value it asked for.
                    _element(2, "Light", "Toggle", (.1, .15, .3, .22), value="0" if dark else "1"),
                    _element(3, "Dark", "Toggle", (.6, .15, .3, .22), value="1" if dark else "0"),
                    _element(4, "Automatic", "Switch", (0, .41, 1, .06), value="0"),
                    _element(5, "Text Size", "Cell", (0, .53, 1, .06)),
                    _element(6, "Bold Text", "Switch", (0, .6, 1, .06), value="0")]
        text = ("Display & Brightness\nAPPEARANCE\nLight\nDark\nAutomatic\nText Size\nBold Text"
                + ("\nDark, selected" if dark else "\nLight, selected"))
        return elements, text

    def search(self, stage, typed):
        if stage == "home":
            elements = [_element(0, "Search", "Button", (.82, .06, .12, .045)),
                        _element(1, "Profile", "Button", (.8, .92, .15, .05))]
            return elements, "For You\nSearch\nProfile"
        if stage == "search":
            elements = [_element(0, "Search", "SearchField", (.1, .06, .7, .05), editable=True,
                                 actions=("TAP", "TYPE_SUBMIT", "TYPE"), value=typed),
                        _element(1, "Cancel", "Button", (.82, .06, .15, .05))]
            return elements, "Search"
        title = f"{self.query.capitalize()} guide"
        elements = [_element(0, title, "Button", (.05, .2, .9, .3)),
                    _element(1, "@getmobster", "Button", (.1, .55, .5, .05))]
        return elements, f"Top results\n{title}\n@getmobster"


class DemoPhone(Driver):
    """A scripted iPhone showing one app. ``pace`` scales the simulated latency (0 in tests)."""

    capabilities = frozenset({"capture_preview"})
    can_type = True

    def __init__(self, bundle, goal, pace=1.0):
        self.bundle, self.pace = bundle, pace
        self.screens = Screens(bundle, goal)
        self.stage = {MESSAGES: "inbox", SETTINGS: "top"}.get(bundle, "home")
        self.typed = ""
        self.lock = threading.Lock()
        self.revision = 0

    def _wait(self, seconds):
        if self.pace > 0:
            time.sleep(seconds * self.pace)

    def snapshot(self):
        elements, text = self.screens.build(self.stage, self.typed)
        return Snapshot(elements, text, 390, 844, SOURCE, bundle_id=self.bundle,
                        revision=f"{self.stage}-{self.revision}")

    def observe(self, timeout=10):
        self._wait(.18)
        with self.lock:
            return self.snapshot()

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self._wait(.22)
        label = target.label if target is not None else ""
        with self.lock:
            self.revision += 1
            stage = self.stage
            if self.bundle == MESSAGES:
                if stage == "inbox" and operation == "TAP" and label == "Alex Rivera":
                    self.stage = "thread"
                elif stage == "thread" and operation in {"TYPE", "TYPE_SUBMIT"}:
                    self.stage, self.typed = "typed", text or ""
                elif stage == "typed" and operation == "TAP" and label == "Send":
                    self.stage = "sent"
            elif self.bundle == SETTINGS:
                if stage == "top" and operation == "SWIPE_UP":
                    self.stage = "scrolled"
                elif stage == "scrolled" and operation == "TAP" and label == "Display & Brightness":
                    self.stage = "display"
                elif stage == "display" and operation == "TAP" and label == "Dark":
                    self.stage = "dark"
            elif self.bundle != WALLET:
                if stage == "home" and operation == "TAP":
                    self.stage = "search"
                elif stage == "search" and operation in {"TYPE", "TYPE_SUBMIT"}:
                    self.stage, self.typed = "results", text or ""

    def capture_preview(self, timeout=3):
        with self.lock:
            stage, typed = self.stage, self.typed
        png = render_screen(self.bundle, stage, typed, self.screens.message, self.screens.query)
        return "data:image/png;base64," + base64.b64encode(png).decode()

    def close(self):
        pass


class DemoChat:
    """The model's stand-in for Mobster's agent."""

    model = MODEL
    provider = "anthropic"  # the usage ledger's name for Mobster's agent's calls; a scripted one costs nothing

    def __init__(self, pace=1.0):
        self.pace = pace
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def _wait(self, seconds):
        if self.pace > 0:
            time.sleep(seconds * self.pace)

    def complete(self, messages, schema, timeout=60, **_):
        text = "\n".join(part.get("text", "") for message in messages for part in (
            message["content"] if isinstance(message["content"], list) else [{"text": message["content"]}]))
        request = (re.search(r"Request: (.*)", text) or [None, ""])[1]
        if "items" in (schema.get("properties") or {}):
            # The task contract: a message to send is the one commit, so Mobster asks before it taps Send; anything
            # else is a look at the screen.
            items = [{"kind": "COMMIT", "app": "Messages", "what": "message to Alex Rivera",
                      "payload": _message(request),
                      "act": "send_message", "count": "1", "condition": "", "quote": request}] \
                if re.search(r"\b(?:text|message|send)\b", request, re.I) else \
                [{"kind": "READ", "app": "", "what": "the screen the task asks for", "payload": "", "act": "none",
                  "count": "", "condition": "", "quote": request}]
            return {"items": items}, {"prompt_tokens": 0, "completion_tokens": 0}
        self._wait(.55)
        self.usage["calls"] += 1
        rows = text.split("Screen elements:\n", 1)[-1].splitlines()

        def row(label):
            return next((line for line in rows if f'"{label}"' in line), None)

        def act(operation, label=None, words=None):
            line = row(label) if label else None
            return {"operation": operation, "target": line.split()[0] if line else None, "target_label": None,
                    "text": words, "app": None}
        answer = None
        if row("Delivered"):
            actions, answer = [act("DONE")], f"Sent “{_message(request)}” to Alex Rivera."
        elif row("Send"):
            actions = [act("TAP", "Send")]
        elif row("Message"):
            actions = [act("TYPE", "Message", _message(request))]
        elif row("Alex Rivera"):
            actions = [act("TAP", "Alex Rivera")]
        elif row("Dark") and re.search(r'"Dark".*\bon\b', row("Dark")):
            actions, answer = [act("DONE")], "Dark Mode is on."
        elif row("Dark"):
            actions = [act("TAP", "Dark")]
        elif row("Display & Brightness"):
            actions = [act("TAP", "Display & Brightness")]
        elif row("Wi-Fi"):
            actions = [act("SWIPE_UP")]
        elif row("Apple Card"):
            actions, answer = [act("DONE")], "Your Wallet has an Apple Card and a Transit card."
        elif any("results" in line.casefold() for line in rows) or row(f"{_query(request).capitalize()} guide"):
            actions, answer = [act("DONE")], f"The top result is the {_query(request).capitalize()} guide."
        elif row("Cancel"):
            actions = [act("TYPE_SUBMIT", "Search", _query(request))]
        else:
            actions = [act("TAP", "Search")]
        return {"thought": "", "plan": "", "notes_add": [], "answer": answer, "checklist_updates": [],
                "actions": actions}, {"prompt_tokens": 0, "completion_tokens": 0}

    def close(self):
        pass


LIGHT = {"bg": (255, 255, 255), "text": (0, 0, 0), "muted": (138, 138, 142), "rule": (216, 216, 220),
         "field": (238, 238, 240), "blue": (10, 132, 255), "bubble": (233, 233, 235)}
DARK = {"bg": (0, 0, 0), "text": (255, 255, 255), "muted": (142, 142, 147), "rule": (56, 56, 58),
        "field": (28, 28, 30), "blue": (10, 132, 255), "bubble": (38, 38, 40)}
ICON_COLORS = [(52, 120, 246), (255, 149, 0), (0, 122, 255), (0, 122, 255), (52, 199, 89), (52, 199, 89),
               (142, 142, 147), (0, 122, 255), (142, 142, 147), (0, 122, 255)]
AVATARS = [(255, 159, 10), (191, 90, 242), (48, 209, 88), (100, 210, 255), (255, 69, 58)]


@lru_cache(maxsize=32)
def render_screen(bundle, stage, typed="", message=DEFAULT_MESSAGE, query="coffee"):
    """A PNG of one scripted screen, 390 x 844 like an iPhone's points."""
    from PIL import Image, ImageDraw, ImageFont

    dark = bundle == SETTINGS and stage == "dark"
    c = DARK if dark else LIGHT
    image = Image.new("RGB", (390, 844), c["bg"])
    draw = ImageDraw.Draw(image)

    def font(size):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow without FreeType
            return ImageFont.load_default()

    small, body, title = font(15), font(17), font(30)
    draw.text((28, 14), "9:41", fill=c["text"], font=small)
    draw.rounded_rectangle((330, 16, 360, 28), radius=3, outline=c["text"], width=2)
    draw.rectangle((333, 19, 352, 25), fill=c["text"])

    if bundle == MESSAGES and stage == "inbox":
        draw.text((16, 50), "Edit", fill=c["blue"], font=body)
        draw.text((20, 88), "Messages", fill=c["text"], font=title)
        draw.rounded_rectangle((16, 130, 374, 164), radius=10, fill=c["field"])
        draw.text((40, 138), "Search", fill=c["muted"], font=body)
        for row, (name, preview) in enumerate(Screens.THREADS):
            y = 186 + row * 84
            draw.ellipse((16, y, 70, y + 54), fill=AVATARS[row % len(AVATARS)])
            draw.text((84, y + 4), name, fill=c["text"], font=body)
            draw.text((84, y + 28), preview[:30], fill=c["muted"], font=small)
            draw.line((84, y + 72, 390, y + 72), fill=c["rule"])
    elif bundle == MESSAGES:
        draw.text((12, 50), "‹", fill=c["blue"], font=title)
        draw.ellipse((170, 44, 220, 94), fill=AVATARS[0])
        draw.text((158, 100), "Alex Rivera", fill=c["text"], font=small)
        draw.line((0, 126, 390, 126), fill=c["rule"])
        draw.rounded_rectangle((16, 170, 270, 222), radius=20, fill=c["bubble"])
        draw.text((32, 186), "See you at 7? I'll grab a table", fill=c["text"], font=small)
        if stage == "sent":
            draw.rounded_rectangle((110, 246, 374, 298), radius=20, fill=c["blue"])
            draw.text((124, 262), message[:30], fill=(255, 255, 255), font=small)
            draw.text((306, 306), "Delivered", fill=c["muted"], font=small)
        draw.rounded_rectangle((52, 756, 374, 792), radius=18, outline=c["rule"], width=2)
        field = typed if stage == "typed" else ""
        draw.text((68, 764), field[:28] or "iMessage", fill=c["text"] if field else c["muted"], font=body)
        if stage == "typed":
            draw.ellipse((340, 760, 368, 788), fill=c["blue"])
        draw.text((18, 758), "+", fill=c["muted"], font=title)
    elif bundle == SETTINGS and stage in {"top", "scrolled"}:
        draw.text((20, 60), "Settings", fill=c["text"], font=title)
        draw.rounded_rectangle((16, 106, 374, 140), radius=10, fill=c["field"])
        draw.text((40, 114), "Search", fill=c["muted"], font=body)
        rows = Screens.ROWS if stage == "top" else Screens.MORE_ROWS
        for row, name in enumerate(rows):
            y = 166 + row * 62
            draw.rounded_rectangle((20, y + 12, 50, y + 42), radius=7,
                                   fill=ICON_COLORS[(row + (0 if stage == "top" else 5)) % len(ICON_COLORS)])
            draw.text((64, y + 16), name, fill=c["text"], font=body)
            draw.text((360, y + 14), "›", fill=c["muted"], font=body)
            draw.line((64, y + 58, 390, y + 58), fill=c["rule"])
    elif bundle == SETTINGS:
        draw.text((12, 50), "‹ Settings", fill=c["blue"], font=body)
        draw.text((100, 86), "Display & Brightness", fill=c["text"], font=body)
        draw.text((24, 128), "APPEARANCE", fill=c["muted"], font=small)
        for column, (name, shade) in enumerate((("Light", (242, 242, 247)), ("Dark", (28, 28, 30)))):
            x = 60 + column * 160
            draw.rounded_rectangle((x, 156, x + 110, 330), radius=14, fill=shade, outline=c["rule"], width=2)
            draw.text((x + 34, 344), name, fill=c["text"], font=body)
            selected = (name == "Dark") == dark
            draw.ellipse((x + 45, 376, x + 65, 396), fill=c["blue"] if selected else c["bg"],
                         outline=c["muted"], width=2)
        for row, name in enumerate(("Automatic", "Text Size", "Bold Text")):
            y = 440 + row * 58
            draw.text((24, y), name, fill=c["text"], font=body)
            draw.line((24, y + 40, 390, y + 40), fill=c["rule"])
        draw.rounded_rectangle((300, 434, 350, 464), radius=15, fill=c["field"])
    elif bundle == WALLET:
        draw.text((20, 60), "Wallet", fill=c["text"], font=title)
        draw.rounded_rectangle((20, 118, 370, 336), radius=18, fill=(40, 40, 40))
        draw.rounded_rectangle((20, 362, 370, 580), radius=18, fill=(10, 132, 255))
    else:
        if stage == "home":
            draw.rectangle((0, 0, 390, 844), fill=(18, 18, 18))
            draw.text((150, 56), "For You", fill=(255, 255, 255), font=body)
            draw.ellipse((150, 330, 240, 420), outline=(255, 255, 255), width=3)
        else:
            draw.rounded_rectangle((16, 50, 310, 90), radius=10, fill=c["field"])
            draw.text((32, 60), (typed or query)[:22] if stage == "results" else "Search", fill=c["text"], font=body)
            if stage == "results":
                draw.rounded_rectangle((20, 160, 370, 420), radius=12, fill=(214, 170, 120))
                draw.text((30, 430), f"{query.capitalize()} guide"[:30], fill=c["text"], font=body)
    draw.rounded_rectangle((128, 826, 262, 831), radius=2, fill=c["text"])
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()
