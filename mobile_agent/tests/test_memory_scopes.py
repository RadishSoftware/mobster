"""Workflow memory is selected only by a run's admitted saved identities."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.accounts.context import AccountProvider
from mobile_agent.api_errors import APIError
from mobile_agent.memory import listener, provider, retrieval
from mobile_agent.memory.model import clean_scope, scope_parts
from mobile_agent.memory.store import MemoryStore

A, B = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
W, X = "111111111111", "222222222222"


def row(ident, scope, pinned=True):
    return {"id": ident, "text": "Use the blue gym", "scope": scope, "kind": "value", "source": "user",
            "pinned": pinned, "updated_at": 1, "last_used_at": None, "confidence": 1}


class ScopeTests(unittest.TestCase):
    def test_scope_grammar_and_unknown_rows_fail_closed(self):
        self.assertEqual(scope_parts(f"profile:{A}/workflow:{W}"), ("profile_workflow", f"{A}/workflow:{W}"))
        self.assertEqual(scope_parts("")[0], "unknown")
        for scope in ("account:" + A, "workflow:ZZZZZZZZZZZZ", f"profile:{A}/workflow:{W}/global"):
            with self.subTest(scope=scope):
                self.assertEqual(scope_parts(scope)[0], "unknown")
                with self.assertRaises(APIError):
                    clean_scope(scope)

    def test_scoped_pins_require_every_admitted_identity(self):
        rows = [row("global", "global"), row("a", f"profile:{A}"), row("b", f"profile:{B}"),
                row("w", f"workflow:{W}"), row("x", f"workflow:{X}"),
                row("both", f"profile:{A}/workflow:{W}"), row("bad", "workflow:bad")]
        chosen = lambda **ctx: {fact["id"] for fact in retrieval.select(rows, f"gym {A} {W}", context=ctx)}
        self.assertEqual(chosen(profile=A, workflow=W), {"global", "a", "w", "both"})
        self.assertEqual(chosen(profile=B, workflow=W), {"global", "b", "w"})
        self.assertEqual(chosen(profile=A, workflow=X), {"global", "a", "x"})
        self.assertEqual(chosen(), {"global"})

    def test_same_words_and_dismissals_are_scoped(self):
        store = MemoryStore(Path(tempfile.mkdtemp(prefix="mobster-memory-scopes-")) / "memory")
        a = store.add_fact("Use the blue gym", f"profile:{A}")
        b = store.add_fact("Use the blue gym", f"profile:{B}")
        self.assertNotEqual(a["id"], b["id"])
        old = store.propose("Remember the garden gate", f"profile:{A}")
        store.resolve_proposal(old["id"], False)
        self.assertIsNone(store.propose("Remember the garden gate", f"profile:{A}"))
        other = store.propose("Remember the garden gate", f"profile:{B}/workflow:{W}")
        self.assertEqual(other["scope"], f"profile:{B}/workflow:{W}")
        _, saved = store.resolve_proposal(other["id"], True)
        self.assertEqual(saved["scope"], other["scope"])
        self.assertIn(f"Saved profile {B} and workflow {W}", store.export_markdown())

    def test_an_orphaned_workflow_fact_matches_only_the_exact_saved_id(self):
        store = MemoryStore(Path(tempfile.mkdtemp(prefix="mobster-memory-scopes-")) / "memory")
        saved = store.add_fact("Use the blue gym", f"workflow:{W}", pinned=True)
        self.assertEqual(retrieval.select(store.all_facts(), "gym", context={"workflow": X}), [])
        # A portable import with the same canonical workflow ID intentionally finds its old facts again.
        self.assertEqual([row["id"] for row in retrieval.select(store.all_facts(), "gym", context={"workflow": W})],
                         [saved["id"]])

    def test_provider_uses_snapshot_and_private_workflow_not_thread_or_extras(self):
        base = SimpleNamespace(extras={"account": {"id": B}, "threadId": W}, account_context={"id": A},
                               workflow_id=W, device_id=None)
        self.assertEqual(provider.context(base), {"profile": A, "workflow": W})
        base.account_context = None
        base.workflow_id = None
        self.assertEqual(provider.context(base), {})

    def test_same_thread_runs_select_only_their_admitted_profile_and_workflow(self):
        store = MemoryStore(Path(tempfile.mkdtemp(prefix="mobster-memory-scopes-")) / "memory")
        saved = store.add_fact("Use the blue gym", f"profile:{A}/workflow:{W}", pinned=True)
        for profile, workflow, expected in ((A, W, True), (B, W, False), (A, X, False), (None, None, False)):
            with self.subTest(profile=profile, workflow=workflow):
                events = []
                ctx = SimpleNamespace(run_id=f"run-{profile}-{workflow}", goal=f"Gym {A} {W}", app_bundle=None,
                                      device_id=None, runtime=None, thread_id="same-thread",
                                      extras={"account": {"id": B}, "threadId": "same-thread"},
                                      account_context={"id": profile} if profile else None, workflow_id=workflow,
                                      emit=events.append)
                blocks = provider.MemoryProvider(store).blocks(ctx)
                self.assertEqual(bool(blocks), expected)
                if expected:
                    self.assertIn(saved["text"], blocks[0].text)
                    self.assertEqual(events, [{"event": "memory_used", "ids": [saved["id"]]}])

    def test_profile_mirrors_never_enter_a_prompt_but_personal_profile_facts_do(self):
        store = MemoryStore(Path(tempfile.mkdtemp(prefix="mobster-memory-scopes-")) / "memory")
        mirror = store.learn("New live profile notes", "preference", f"profile:{A}", "profile",
                             key="profile:notes", authoritative=True)["fact"]
        store.update_fact(mirror["id"], text="An edited mirror", pinned=True)
        mirror = store.learn("Newer live profile notes", "preference", f"profile:{A}", "profile",
                             key="profile:notes", authoritative=True)["fact"]
        self.assertEqual(mirror["source"], "profile")
        personal = store.add_fact("Use a warm greeting", f"profile:{A}", pinned=True)
        learned = store.learn("Profile menu moved", "place", f"profile:{A}", "run")["fact"]
        events = []
        ctx = SimpleNamespace(run_id="snapshot-run", goal="greeting profile menu", app_bundle=None,
                              device_id=None, runtime=None, thread_id=None, extras={},
                              account_context={"id": A, "handle": "getmobster",
                                               "app": {"name": "Messages", "bundleId": "com.apple.MobileSMS"},
                                               "notes": "Old admitted notes"}, workflow_id=None,
                              emit=events.append)
        blocks = provider.MemoryProvider(store).blocks(ctx)
        self.assertIn("Old admitted notes", AccountProvider(ctx.account_context).blocks(ctx)[0].text)
        self.assertEqual(len(blocks), 2)
        self.assertIn(personal["text"], blocks[0].text)
        self.assertFalse(blocks[0].untrusted)
        self.assertIn(learned["text"], blocks[1].text)
        self.assertTrue(blocks[1].untrusted)
        self.assertIn(f"<<<\n- {learned['text']}\n>>>", harness_api.render_block(blocks[1]))
        self.assertNotIn("<<<", harness_api.render_block(blocks[0]))
        self.assertNotIn(mirror["text"], "".join(block.text for block in blocks))
        self.assertEqual(events, [{"event": "memory_used", "ids": [personal["id"], learned["id"]]}])
        self.assertEqual([fact["id"] for fact in retrieval.select(store.all_facts(), ctx.goal,
                                                                   context={"profile": A})],
                         [personal["id"], learned["id"]])

    def test_suggestions_do_not_downgrade_an_unadmitted_profile(self):
        store = MemoryStore(Path(tempfile.mkdtemp(prefix="mobster-memory-scopes-")) / "memory")
        suggest = listener.ProposalListener(lambda: store)
        run = SimpleNamespace(id=A, origin="app", extras={"threadId": None}, account_context={"id": A}, workflow_id=W)
        with patch.object(listener, "publish"):
            (proposal,) = suggest.consider(run, "Remember that my gym is the blue one")
            self.assertEqual(proposal["scope"], f"profile:{A}/workflow:{W}")
            run.account_context = None
            run.extras = {"account": {"id": B}}
            self.assertEqual(suggest.consider(run, "Remember that my gym is the blue one"), [])
            run.origin = "api"
            self.assertEqual(suggest.consider(run, "Remember that my gym is the blue one"), [])


if __name__ == "__main__":
    unittest.main()
