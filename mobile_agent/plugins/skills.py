"""A plugin's skills (skills/<name>/SKILL.md) as task context: the instructions of a skill whose description matches the
task's words, read from the plugin's installed copy when the task starts."""

import re

from ..harness_api import UNATTENDED_ORIGINS, ContextBlock
from . import manifest
from . import service as plugin_service

MAX_SKILLS = 2
SKILL_CHARS = 4000
MIN_SHARED = 2
STOP = frozenset("""about above after again also and any are because been before being below between both but can
could did does doing down during each for from further had has have having her here hers him his how into its itself
just more most not now off once only other our out over own same she should some such than that the their them then
there these they this those through too under until very was were what when where which while who whom why will with
would you your yours use uses using task tasks phone iphone app apps mobster""".split())


def words(text):
    return {w for w in re.findall(r"[a-z0-9]{3,}", str(text or "").casefold()) if w not in STOP}


def matches(skill, goal):
    folded = " ".join(str(goal or "").casefold().split())
    name = " ".join(re.split(r"[-_\s]+", skill["name"].casefold())).strip()
    if name and len(name) >= 4 and name in folded:
        return True
    return len(words(skill["description"]) & words(goal)) >= MIN_SHARED


class SkillsProvider:
    key = "plugins"
    max_chars = MAX_SKILLS * (SKILL_CHARS + 200)

    def __init__(self, service):
        self.service = service

    def blocks(self, ctx):
        out = []
        for plugin, skill, folder in self.service.skills():
            if len(out) >= MAX_SKILLS:
                break
            if not matches(skill, ctx.goal):
                continue
            try:
                text = (folder / skill["file"]).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            _, body = manifest.frontmatter(text)
            body = body.strip()
            if len(body) > SKILL_CHARS:
                body = body[:SKILL_CHARS] + "\n…"
            if body:
                # Not fenced as data: only the person installs a plugin (the Mac app's session), and nothing a phone
                # shows or a tool returns can install one or change its text.
                out.append(ContextBlock(key=self.key, title=f"Instructions from the plugin {plugin} you installed "
                                                            f"(its skill {skill['name']})", text=body, stable=True))
        return tuple(out)

    def turn_blocks(self, ctx, turn):
        return ()


def provider_factory(ctx):
    if ctx is None or getattr(ctx, "origin", None) in UNATTENDED_ORIGINS or getattr(ctx, "engine", None) != "smart":
        return None
    service = plugin_service.service_for(getattr(ctx, "runtime", None))
    if service is None:
        return None
    try:
        if not service.skills():
            return None
    except Exception:  # noqa: BLE001 -- a journal that can't be read adds nothing
        return None
    return SkillsProvider(service)
