"""The terminal UI's transcript widgets: a task's steps, the phone panel, the approval card and the composer."""

import time

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Static

from ..narrate import Narrator, Step, approval_title, verbs
from . import render
from .render import ACCENT, FAINT, MUTED, TEXT


def ago(timestamp):
    delta = max(0, time.time() - timestamp)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return time.strftime("%b %-d", time.localtime(timestamp))


def clock(ms):
    """A fixed-width running clock: 0:05, 1:28, 12:04 (the status line never shifts as digits change)."""
    total = int((ms or 0) // 1000)
    return f"{total // 60:>2}:{total % 60:02d}"


class ItemView(Static):
    """One narrated item (a step, an approval, a note, the outcome), redrawn when it changes."""

    def __init__(self, item, run_view, number=None):
        super().__init__(classes=f"item {type(item).__name__.lower()}")
        self.item, self.run_view, self.number = item, run_view, number

    def redraw(self, frame=0):
        self.update(render.item_renderable(self.item, expanded=self.app.expanded, frame=frame,
                                           narrator=self.run_view.narrator, number=self.number))

    def on_mount(self):
        self.redraw()

    @property
    def live(self):
        return isinstance(self.item, Step) and self.item.state in {"thinking", "acting"}


class RunView(Vertical):
    """One task in the transcript: the prompt line, then its items as they arrive."""

    def __init__(self, run, apps=None, past=False):
        super().__init__(classes="run")
        self.run = run
        self.narrator = Narrator(apps)
        self.views = {}
        self.past = past
        self.steps = 0

    def compose(self):
        yield Static(self.header(), classes="task")

    def header(self):
        text = Text()
        text.append("❯ ", style=f"bold {ACCENT}")
        text.append(self.run.goal, style=f"bold {TEXT}")
        app = self.run.app.get("name") if self.run.app.get("bundleId") else None
        meta = f"  {app}" if app else ""
        if self.past:
            meta += f", {ago(self.run.created_at)}, {self.run.id}"
        text.append(meta, style=FAINT)
        return text

    def note(self, text, style=MUTED):
        """A line in this task that isn't an event: a message you sent while it worked."""
        line = Static(Text(text, style=style), classes="item note")
        self.mount(line)

    def feed(self, event):
        changed = self.narrator.feed(event)
        for item in changed:
            if getattr(item, "merged_into", None) is not None:
                # Folded into the stalled step before it: one line with a count instead of two.
                view = self.views.pop(item.key, None)
                if view is not None:
                    view.remove()
                    self.steps -= 1
                continue
            view = self.views.get(item.key)
            if view is None:
                number = None
                if isinstance(item, Step):
                    self.steps += 1
                    number = self.steps
                view = ItemView(item, self, number)
                self.views[item.key] = view
                self.mount(view)
            else:
                view.redraw(self.app.frame)
        return changed

    def redraw(self):
        for view in self.views.values():
            view.redraw(self.app.frame)


class PhonePanel(Vertical):
    """The phone beside the transcript."""

    def __init__(self, image_class=None, **kwargs):
        super().__init__(**kwargs)
        self.image_class = image_class
        self.add_class(*(("image",) if image_class else ("text", "empty")))

    def compose(self):
        yield Static("Phone" if self.image_class else "Screen", id="phone-title")
        if self.image_class:
            yield self.image_class(id="phone-image")
        else:
            yield Static(render.screen_outline(None, 30, 10), id="phone-outline")
        yield Static("", id="phone-caption")

    def show_image(self, png, caption=""):
        if png:
            import io

            from PIL import Image
            try:
                self.query_one("#phone-image").image = Image.open(io.BytesIO(png))
            except Exception:
                pass
        self.query_one("#phone-caption", Static).update(Text(caption, style=FAINT))

    outline = None

    def show_outline(self, screen, target_id=None, caption=""):
        # Until a task has read a screen there is nothing to show: give the room to the transcript.
        empty = not (screen and screen.get("elements"))
        was_empty = self.has_class("empty")
        self.set_class(empty, "empty")
        self.outline = (screen, target_id, caption)
        if was_empty and not empty:
            self.call_after_refresh(self.draw_outline)  # sized once it is laid out
        self.draw_outline()

    def draw_outline(self):
        if self.outline is None:
            return
        screen, target_id, caption = self.outline
        width = max(16, self.size.width - 2)
        height = max(6, self.size.height - 4)
        self.query_one("#phone-outline", Static).update(render.screen_outline(screen, width, height, target_id))
        self.query_one("#phone-caption", Static).update(Text(caption, style=FAINT))

    def on_resize(self, event):
        if not self.image_class:
            self.draw_outline()


class ApprovalCard(VerticalScroll, can_focus=True):
    """Mobster is waiting for you."""

    BINDINGS = [
        Binding("y", "answer('yes')", show=False), Binding("n", "answer('no')", show=False),
        Binding("a", "answer('always')", show=False),
        Binding("escape", "answer('no')", show=False), Binding("enter", "answer('enter')", show=False),
        Binding("up", "move(-1)", show=False), Binding("down", "move(1)", show=False),
    ] + [Binding(str(n), f"answer('{n}')", show=False) for n in range(1, 7)]

    def __init__(self):
        body = Static()
        super().__init__(body, id="approval")
        self.body = body
        self.request = None
        self.cursor = 0

    def options(self):
        request = self.request or {}
        if request.get("kind", "action") != "action" and request.get("choices"):
            return [(c["id"], c["label"]) for c in request["choices"]] + [("__no", "Stop the task")]
        yes, no = verbs(request)
        if request.get("act") == "use app" and request.get("rememberable"):
            # One card for the task's apps: Allow covers this task, Always allow keeps them (Settings › Safety).
            return [("__yes", yes), ("__always", request.get("rememberLabel") or "Always allow"), ("__no", no)]
        return [("__yes", yes), ("__no", no)]

    def ask(self, request):
        self.request, self.cursor = request, 0
        if self.is_mounted:
            self.scroll_home(animate=False)
        if request.get("defaultChoice"):
            ids = [key for key, _ in self.options()]
            self.cursor = ids.index(request["defaultChoice"]) if request["defaultChoice"] in ids else 0
        self.redraw()

    def redraw(self):
        request = self.request or {}
        text = Text()
        what = request.get("question") if request.get("kind", "action") != "action" else None
        if not what:
            what = approval_title(request)
        text.append(what + "\n", style=f"bold {TEXT}")
        if request.get("text"):
            text.append(f"“{request['text']}”\n", style=TEXT)
        if request.get("kind", "action") == "action":
            from ..smart_run import reassurance
            text.append(reassurance(request) + "\n", style=FAINT)
        elif request.get("limits"):
            text.append(f"{request['limits']}\n", style=FAINT)
        elif request.get("act") == "use app" and request.get("reason"):
            text.append(f"{request['reason']}\n", style=FAINT)  # what Allow and Always allow each keep
        text.append("\n")
        for index, (key, label) in enumerate(self.options()):
            selected = index == self.cursor
            text.append("❯ " if selected else "  ", style=f"bold {ACCENT}")
            text.append(f"{index + 1}. {label}", style=f"bold {TEXT}" if selected else MUTED)
            hint = {"__yes": "   y", "__always": "   a", "__no": "   n or esc"}.get(key, "")
            text.append(hint + "\n", style=FAINT)
        expires = request.get("expiresAt")
        if expires:
            left = max(0, expires / 1000 - time.time())
            no = self.options()[-1][1]
            text.append(f"{no} in {int(left // 60)}:{int(left % 60):02d} if nobody answers", style=FAINT)
        self.body.update(text)

    def action_move(self, delta):
        self.cursor = (self.cursor + delta) % len(self.options())
        self.redraw()

    def action_answer(self, key):
        options = self.options()
        if key == "enter":
            choice = options[self.cursor][0]
        elif key == "yes":
            choice = "__yes" if any(k == "__yes" for k, _ in options) else options[self.cursor][0]
        elif key == "no":
            choice = "__no"
        elif key == "always":
            if not any(k == "__always" for k, _ in options):
                return
            choice = "__always"
        else:
            index = int(key) - 1
            if index >= len(options):
                return
            choice = options[index][0]
        self.app.answer_approval(self.request, choice)


class PromptInput(Input):
    """The task prompt. The app sees popup, recall and help keys before the text does."""

    async def _on_key(self, event: events.Key):
        if self.app.prompt_key(event.key, event.character):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


PLACEHOLDER = "What should Mobster do on your iPhone?"
NEXT_PLACEHOLDER = "What should Mobster do next?"
STEER_PLACEHOLDER = "Tell Mobster something while it works…"


class Composer(Horizontal):
    def compose(self):
        yield Static("", id="chip")
        yield PromptInput(placeholder=PLACEHOLDER, id="prompt", select_on_focus=False)
