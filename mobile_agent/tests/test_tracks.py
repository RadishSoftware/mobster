import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import api_routes, harness_api, journal, tracks
from mobile_agent.api_routes import Response, Route
from mobile_agent.mcp_server import registry as mcp_registry
from mobile_agent.server import Runtime
from mobile_agent.tests.seam_support import isolate

LEFT_OUT = {track for track in tracks.TRACKS if importlib.util.find_spec(f"mobile_agent.{track}") is None}


class Provider:
    key, max_chars = "notes", 100

    def blocks(self, ctx):
        return ()

    def turn_blocks(self, ctx, turn):
        return ()


class McpTools:
    name = "fake"

    def definitions(self, toolset):
        return []

    def annotations(self):
        return {}

    def instructions(self):
        return None

    def call(self, name, arguments, call, toolset):
        return None


def register_everything_then_raise(api):
    api.register_context_provider(lambda ctx: Provider(), order=10)
    api.register_tool(lambda ctx: None, order=10)
    api.register_run_field("threadId", lambda value, runtime: value)
    api.register_run_listener(object())
    api.register_pre_run(lambda ctx, driver, approve: None, order=0)
    api.register_frontier_options(lambda ctx: {})
    api.register_service(lambda runtime: None, lambda runtime: None)
    api.register_thread_sink(lambda thread, item: item, lambda thread, item, changes: changes)
    api_routes.register(Route("threads", "GET", "/api/threads", lambda request: Response.json({})))
    journal.register_schema("threads", 1, lambda conn, version: None)
    mcp_registry.register_provider(McpTools())
    raise RuntimeError("halfway")


class TrackLoaderTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_a_track_that_raises_halfway_leaves_nothing_registered(self):
        self.assertFalse(tracks.register_one("threads", register_everything_then_raise))
        self.assertEqual(tracks.STATUS["threads"], "error: RuntimeError")
        self.assertEqual(harness_api._snapshot(), ([], [], {}, [], [], [], [], [], {}))
        self.assertEqual(api_routes._routes, [])
        self.assertEqual(journal.SCHEMAS, {})
        self.assertEqual(mcp_registry._providers, [])
        self.assertIsNone(harness_api.post_thread_item("3f9c2a1b7d0e", {"kind": "note"}))

    def test_a_track_that_registers_keeps_it_and_owns_it(self):
        def register(api):
            api.register_run_field("threadId", lambda value, runtime: value)
            journal.register_schema("threads", 1, lambda conn, version: None)

        self.assertTrue(tracks.register_one("threads", register))
        self.assertEqual(tracks.STATUS["threads"], "ok")
        self.assertIn("threadId", harness_api.run_fields())
        self.assertEqual(journal.SCHEMA_OWNERS["threads"], "threads")
        tracks.disable("threads", "error: newer data")
        self.assertNotIn("threadId", harness_api.run_fields())

    def test_one_failing_track_does_not_stop_the_others(self):
        calls = []

        def importers():
            def bad():
                return SimpleNamespace(register=register_everything_then_raise)

            def good():
                return SimpleNamespace(register=lambda api: calls.append("memory"))

            def missing():
                raise ImportError("no such module")

            def left_out():
                raise ModuleNotFoundError("No module named 'mobile_agent.remote'", name="mobile_agent.remote")

            def broken_dependency():
                raise ModuleNotFoundError("No module named 'websockets'", name="websockets")
            return (("threads", bad), ("memory", good), ("wireless", missing), ("remote", left_out),
                    ("testkit", broken_dependency))

        with patch.object(tracks, "_importers", importers), patch.object(tracks, "_loaded", False):
            tracks.load()
            tracks.load()
        self.assertEqual(calls, ["memory"])
        self.assertEqual(tracks.STATUS["threads"], "error: RuntimeError")
        self.assertEqual(tracks.STATUS["memory"], "ok")
        self.assertEqual(tracks.STATUS["wireless"], "error: ImportError")
        self.assertEqual(tracks.STATUS["remote"], "absent")
        self.assertEqual(tracks.STATUS["testkit"], "error: ModuleNotFoundError")


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_status_lists_every_extension_ok_on_an_in_memory_journal(self):
        config = SimpleNamespace(wda_url=None, session=None, enable_live=False, port=8765, state_db=None,
                                 env_file=None, data_dir=None)
        with patch("mobile_agent.server.Runtime.make_video"), patch("mobile_agent.server.Fleet"):
            runtime = Runtime(config)
        try:
            self.assertIsNone(runtime.journal.lease)
            extensions = runtime.status()["extensions"]
            self.assertEqual(set(extensions), set(tracks.TRACKS))
            self.assertEqual({t: v for t, v in extensions.items() if v != "ok"}, {t: "absent" for t in LEFT_OUT})
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
