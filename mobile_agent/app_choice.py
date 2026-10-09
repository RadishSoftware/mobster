"""Which app a task means: the app it names or opens, the channel a message goes through, or the verb's own app.

Shared by the terminal UI (its app chip) and `mobster run --demo`, so neither needs the UI library to decide.
"""

import re

# When a task names no app, its opening verb can ("Text Alex…" is Messages), and so can a few
# settings words anywhere ("…Display & Text Size" is Settings). Verbs count only at the start:
# "Text Size" in the middle of a Settings task must not pick Messages.
APP_VERBS = [
    (r"(text|message|imessage)\s", "messages"), (r"e-?mail\s", "mail"), (r"(call|dial)\s", "phone"),
    (r"facetime\s", "face-time"), (r"remind me\b", "reminders"), (r"(set|start) (an? )?(alarm|timer)\b", "clock"),
    (r"(take|write|make) a note\b", "notes"), (r"(play|queue)\s", "music"),
    (r"(search|google|look up)\s", "safari"),
]
APP_WORDS = [
    (r"\b(dark mode|light mode|wi-?fi|bluetooth|airplane mode|brightness|accessibility|text size|"
     r"display|wallpaper|ringtone|notifications|screen time|focus mode|general|about this|ios version|"
     r"software update|storage|battery|cellular|hotspot|privacy|passwords?|sounds?)\b", "settings"),
    (r"\b(weather|forecast)\b", "weather"), (r"(https?://|www\.)", "safari"),
]


# App names that are also everyday words ("heading home", "the news", "Find my boarding pass"). They count only
# when the task clearly means the app: written capitalised mid-sentence ("…in my Photos"), after
# in/open/launch/from/using ("open music"), or before "app" ("the home app"); and an opening verb ("Text Sam…")
# wins over them.
COMMON_WORD_APPS = {"home", "news", "photos", "music", "clock", "files", "phone", "mail", "find my"}
# ...or when what follows is what the app finds: "Find my iPhone", "Find my AirPods", "Find my son's phone".
_FINDS = {"find my": r"\bfind my\s+(?:[\w-]+['’]s\s+)?(?:iphone|ipad|ipod|airpods|apple watch|watch|mac|macbook|"
                     r"keys|airtags?|phone(?!\s+numbers?\b)|devices?)(?![\w-])"}
_NAMED_AS_APP = r"(?:\b(?:in|open|launch|from|using|into)\s+(?:the\s+|my\s+)?){name}(?![\w-])|(?<![\w-]){name}\s+app\b"
# Verbs whose object is a person or a message: the rest of the task is what to say, so an app named in it
# ("Message Sam about the camera settings") is not where it runs, unless it names the channel ("…on WhatsApp").
MESSAGE_VERB_APPS = {"messages", "mail", "phone", "face-time", "reminders", "notes"}
_CHANNEL = r"\b(?:on|via|through|over)\s+(?:the\s+|my\s+)?{name}(?![\w-])"
# ...or unless the verb's object is another app: "Call an Uber", "Call me a Lyft", "Message my Airbnb host", "Text
# the DoorDash driver". The ride, the host and the driver are reached through that app. The verbs' own apps and
# the common words stay what is said or sent ("Email the notes to Sam", "Text my photos to Mom").
_OBJECT = r"^\s*(?:me\s+)?(?:(?:an?|the|my|our|your)\s+)?{name}(?![\w-])"


def _names(name, lowered):
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", lowered) is not None


def _named_as_app(app, lowered, pattern=_NAMED_AS_APP):
    name = app["name"].casefold()
    return len(name) >= 3 and re.search(pattern.format(name=re.escape(name)), lowered) is not None


def _clearly_the_app(app, goal, lowered):
    if _named_as_app(app, lowered):
        return True
    written = re.search(rf"(?<![\w-]){re.escape(app['name'])}(?![\w-])", goal)
    return written is not None and written.start() > 0


def infer_app(goal, apps):
    """The app a task opens or works in ("Open Photos", "…in Wallet"), longest name first; else, for a
    messaging verb, the channel it names ("Message Sam on WhatsApp"), the app that is its object ("Call an
    Uber", "Message my Airbnb host") or the verb's own app ("Message Sam about…" is Messages); else any app it
    names; else the one another opening verb points at ("Play…" is Music); else a common-word name used as the
    app ("…in my Photos"); else the one its settings words point at; else None.

    So an everyday phrase that is also an app's name ("Find my boarding pass", "…in Wallet") gives way to the
    app the task says it works in, or to none.
    """
    goal = goal.strip()
    lowered = goal.casefold()
    by_length = sorted(apps, key=lambda a: -len(a["name"]))
    named = next((app for app in by_length if _named_as_app(app, lowered)), None)
    if named is not None:
        return named
    opening = re.sub(r"^(please|can you|could you)\s+", "", lowered)

    def hinted(patterns):
        for pattern, app_id in patterns:
            match = re.search(pattern, opening)
            if match:
                app = next((a for a in apps if a["id"] == app_id), None)
                if app is not None:
                    return app, match
        return None, None

    verb, said = hinted([(rf"^{pattern}", app_id) for pattern, app_id in APP_VERBS])
    if verb is not None and verb["id"] in MESSAGE_VERB_APPS:
        rest = opening[said.end():]
        channel = next((app for app in by_length if _named_as_app(app, lowered, _CHANNEL)), None)
        target = channel or next((app for app in by_length if app["id"] not in MESSAGE_VERB_APPS
                                  and app["name"].casefold() not in COMMON_WORD_APPS
                                  and _named_as_app(app, rest, _OBJECT)), None)
        return target or verb
    common = []
    for app in by_length:
        name = app["name"].casefold()
        if len(name) >= 3 and _names(name, lowered):
            if name not in COMMON_WORD_APPS or name in _FINDS and re.search(_FINDS[name], lowered):
                return app
            common.append(app)
    if verb is not None:
        return verb
    named = next((app for app in common if _clearly_the_app(app, goal, lowered)), None)
    return named if named is not None else hinted(APP_WORDS)[0]


def find_app(name, apps):
    """The app ``name`` names exactly: its id, its name in any case (spaces optional: "facetime"), or its
    bundle ID. None when nothing matches."""
    wanted = (name or "").strip().casefold()
    if not wanted:
        return None
    for app in apps:
        if wanted in {app["id"].casefold(), app["name"].casefold(), app["name"].casefold().replace(" ", ""),
                      (app.get("bundleId") or "").casefold()}:
            return app
    return None


def mention_rank(app, query):
    """How well ``query`` (the letters after @) matches an app, best first: its name or id starts with them
    (0), a word of its name does (1), its name holds them (2), its bundle ID does (3). None: no match."""
    name = app["name"].casefold()
    if name.startswith(query) or name.replace(" ", "").startswith(query) or app["id"].casefold().startswith(query):
        return 0
    if any(word.startswith(query) for word in re.split(r"[\s-]+", name)):
        return 1
    if query in name:
        return 2
    if query in (app.get("bundleId") or "").casefold():
        return 3
    return None


# An @word in the prompt: "@messages", "@com.burbn.instagram", "@nasa". A trailing dot ends the sentence.
MENTION = re.compile(r"(?:^|\s)@([\w-]+(?:\.[\w-]+)*)")
