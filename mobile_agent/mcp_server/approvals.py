"""What an outside agent may do on a real iPhone: the user's permission rules, the app in front,
and the cancelled call."""

import dataclasses
from types import SimpleNamespace

from ..journal import JournalError
from .action_journal import record_action
from .session import node_name
from .support import ToolError, quote


class ApprovalTools:
    @staticmethod
    def _real_phone(session):
        record = getattr(session.run, "record", None)
        if session.mode != "direct" or not isinstance(record, dict) or record.get("kind") == "simulator":
            return None
        return record

    def _permit(self, session, op, node, element, snapshot, call, text=None, *, forced=False):
        self._check_action_cancelled(session, call)
        record = self._real_phone(session)
        if record is None:
            return False
        from ..permissions import outside
        from ..permissions.gate import action_from
        from ..task_policy import springboard_keypad
        if springboard_keypad(snapshot):
            raise ToolError("The iPhone is showing its passcode keypad. Ask the user to unlock it.")
        target = element if element is not None else SimpleNamespace(
            label=node_name(node) or "", role=(node.role or "").removeprefix("XCUIElementType"), value="")
        action = action_from(op, target, snapshot, text=text)
        if node is not None:
            action = dataclasses.replace(action, hint=f"{action.hint} {node.placeholder or ''} {node.identifier or ''}")
        try:
            asked = outside.check_action(action, device=record.get("id"), client_name=self.client_name(),
                                         **({"forced": True} if forced else {}))
        except outside.Refused as refused:
            raise ToolError(str(refused)) from None
        self._check_action_cancelled(session, call)
        if asked:
            self.log(f"Run {session.run_id}: the user approved {op.lower()} in the Mobster app.")
        return asked

    def _rebind(self, session, element, snapshot, call):
        from ..frontier import CHANGED_WHILE_ASKING, FrontierAgent, binding, same_binding
        self._check_action_cancelled(session, call)
        self._permit_app(session, snapshot.bundle_id)
        fresh = session.run.driver.observe(timeout=10)
        self._check_action_cancelled(session, call)
        if element is None:
            if fresh.fingerprint != snapshot.fingerprint:
                raise ToolError(CHANGED_WHILE_ASKING + " Call screen, then make the call again.")
            return None, fresh
        found = FrontierAgent._find(fresh, element.label, element.role, element.locator)
        if found is None or not same_binding(binding(snapshot, element), binding(fresh, found)):
            raise ToolError(CHANGED_WHILE_ASKING + " Call screen, then make the call again.")
        return found, fresh

    def _action_cancelled(self, session, call):
        return call.stop.is_set() or session.cancelled.is_set() or self._closing.is_set()

    def _check_action_cancelled(self, session, call):
        # An approval can take minutes: a call cancelled meanwhile must not act when it comes back.
        if self._action_cancelled(session, call):
            raise ToolError("The action was cancelled before dispatch. Call screen before trying again.")

    def _permit_alert(self, session, alert, action, button, call):
        if self._real_phone(session) is None or (action == "dismiss" and not button):
            return
        from ..state import Element, Snapshot
        pressed = button or "OK"
        texts = " ".join([alert.get("text") or ""] + list(alert.get("buttons") or ()))[:450]
        sheet = Element("a0", (alert.get("text") or "")[:450], "Alert", (.1, .3, .8, .4))
        target = Element("a1", pressed[:450], "Button", (.12, .6, .76, .05))
        bundle = getattr(session.run.driver.observe(timeout=10), "bundle_id", "") or ""
        shown = Snapshot([sheet, target], texts, 400, 800, "alert", bundle_id=bundle)
        if self._permit(session, "TAP", None, target, shown, call):
            # `shown` is a drawing of the alert, not a read, so the fresh read is compared to the alert itself.
            self._permit_app(session, bundle)
            fresh = session.run.driver.observe(timeout=10)
            self._check_action_cancelled(session, call)
            if getattr(fresh, "bundle_id", "") != bundle or self._alert(session.run.driver) != alert:
                raise ToolError("The alert changed while approval waited. Call screen before another action.")

    def _check_app(self, session, bundle):
        record = self._real_phone(session)
        if record is None:
            return None
        from ..permissions import outside
        try:
            outside.check_app(bundle, device=record.get("id"), client_name=self.client_name())
        except outside.Refused as refused:
            return refused
        return None

    def _permit_app(self, session, bundle):
        refused = self._check_app(session, bundle)
        if refused is not None:
            raise ToolError(str(refused).replace("Nothing was done.", "Nothing was opened.")) from None

    def _permit_code(self, session, bundle, field_name, snapshot, call):
        self._check_action_cancelled(session, call)
        record = self._real_phone(session)
        if record is None:
            return snapshot
        from ..permissions import outside
        try:
            outside.check_code(bundle, field_name, device=record.get("id"), client_name=self.client_name())
        except outside.Refused as refused:
            raise ToolError(str(refused)) from None
        return self._rebind(session, None, snapshot, call)[1]

    def _front(self, session):
        if self._real_phone(session) is None:
            return
        driver = session.run.driver
        try:
            # The foreground app alone, without reading the accessibility tree, when the driver can say it.
            active_app = getattr(driver, "active_app", None)
            bundle = (active_app(timeout=10) if callable(active_app)
                      else getattr(driver.observe(timeout=10), "bundle_id", ""))
            bundle = bundle.strip() if isinstance(bundle, str) else ""
        except Exception:
            bundle = ""
        if not bundle:
            session.forget_outline()
            raise ToolError("Mobster couldn't identify the app in front, so it stopped. Call screen before "
                            "another action.")
        if bundle == "com.apple.springboard":
            return
        refused = self._check_app(session, bundle)
        if refused is None:
            return
        try:
            gone = self._leave(session, bundle)
        finally:
            session.forget_outline()
        raise ToolError(self._front_words(refused, bundle, session, gone))

    @staticmethod
    def _leave(session, bundle):
        # Only an acknowledged press that left the app in front is tried again; an unknown outcome never is.
        driver = session.run.driver
        for _ in range(2):
            try:
                with record_action(session.run.run_dir, "HOME"):
                    driver.call("POST", "/wda/pressButton", {"name": "home"}, timeout=10)
            except JournalError:
                raise
            except Exception:
                return False
            try:
                if (getattr(driver.observe(timeout=5), "bundle_id", "") or "") != bundle:
                    return True
            except Exception:
                return False
        return False

    def _front_words(self, refused, bundle, session, gone):
        from ..permissions import outside
        name = outside.app_name(bundle, (self._real_phone(session) or {}).get("id"))
        state = getattr(refused, "state", None)
        if state == "pending":
            minutes = getattr(refused, "minutes", None)
            why = (f"{name} is in front, and Mobster is waiting for the user to allow it: the request is open in the "
                   f"Mobster app" + (f" for about {minutes} more minutes" if minutes else "") + ".")
            ask = (f" Tell the user: they can allow {name} in the Mobster app (then call launch_app {bundle}), or ask "
                   "you to use Safari instead.")
        elif state == "denied":
            why = f"{name} is in front, and the user declined to let Mobster use it."
            ask = f" Don't open {name} again unless the user says to; they can ask you to use Safari instead."
        else:
            why = f"{name} is in front. {refused}".replace("Nothing was done.", "").rstrip()
            ask = ""
        moved = (" Mobster pressed Home and read nothing in it." if gone else
                 f" Mobster pressed Home and read nothing in it, but the phone still reports {name} "
                 "in front: call home.")
        return (why + moved + ask + (" Until then" if state == "pending" else " To carry on") + ", call home (it needs "
                "no approval), then launch_app com.apple.mobilesafari or another app the user has allowed.")

    def _refuse_grant(self, session, alert, action, button):
        # A privacy grant is the owner's call: it asks in every permission mode, and MCP has nobody to ask.
        from ..wda.system_dialogs import GRANTS, classify, fold
        if not self._on_phone(session):
            return
        dialog = classify(alert.get("text") or "", alert.get("buttons") or [])
        if not dialog.grant or (action == "dismiss" and not button):
            return
        deny = dialog.deny_button()
        pressing = button or dialog.allow_button()
        if pressing is not None and deny is not None and fold(pressing).casefold() == fold(deny).casefold():
            return
        what = GRANTS.get(dialog.kind, "get access")
        raise ToolError(f"iOS is asking whether {dialog.app or 'an app'} may {what}. On a real iPhone that choice is "
                        f"the owner's: ask the user to answer it on the phone, or press {quote(deny or 'Don’t Allow')} "
                        "with alert dismiss.")

    def _outside_notifications(self, session, rows):
        record = self._real_phone(session)
        if record is None:
            return rows, 0
        from ..permissions import outside, policy
        try:
            apps = policy.from_file().apps
            names = set()
            for bundle in apps.blocked | apps.outside_blocked:
                name = outside.app_name(bundle, record.get("id"))
                names.add(name.casefold())
                if name == bundle:
                    names.add(bundle.rsplit(".", 1)[-1].casefold())
        except Exception:  # noqa: BLE001 -- unsure which apps are kept out: nothing is shown
            return [], len(rows)
        if not names:
            return rows, 0
        # A row with no app name could be a kept-out app's, so it is dropped too.
        kept = [row for row in rows if (row.app or "").strip().casefold() not in names | {""}]
        return kept, len(rows) - len(kept)
