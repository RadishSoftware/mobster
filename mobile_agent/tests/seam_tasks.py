"""A whole Smart task through Runtime.work for the seam tests, with a scripted model and phone (test_server_engine's
fakes)."""

import os
from unittest.mock import patch

from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.test_engines import COMPOSE, Script, item, sent
from mobile_agent.tests.test_server_engine import Base, SmartTaskTests, jpeg


class SmartBase(Base):
    Phone = SmartTaskTests.Phone

    def setUp(self):
        super().setUp()
        isolate(self)
        session = patch("mobile_agent.server.resolve_wda_session", return_value="s")
        session.start()
        self.addCleanup(session.stop)
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"

    def script(self, steps=None, items=None, answer="Done."):
        steps = steps if steps is not None else [[("DONE", None, None)], [("DONE", None, None)]]
        return Script(steps, items if items is not None else [item(kind="READ", act="none", what="Sam",
                                                                   quote="Sam")], answer=answer)

    def work(self, runtime, run, script, screens=None, open_driver=None):
        """Run ``run`` to its end on the scripted phone; returns the phone."""
        phone = self.Phone(screens or [COMPOSE] * 8)
        frames = iter([jpeg()] * 50)
        patches = [patch("mobile_agent.server.build_target_driver", return_value=phone),
                   patch("mobile_agent.server.prepare_wda_phone"),
                   patch("mobile_agent.engines.build_client", return_value=script),
                   patch("mobile_agent.frontier.video_frame", side_effect=lambda *a, **k: next(frames, None))]
        if open_driver is not None:
            patches.append(patch.object(type(runtime), "open_driver", open_driver))
        for p in patches:
            p.start()
        self.worker.stop()
        try:
            runtime.work(run)
        finally:
            self.worker.start()
            for p in reversed(patches):
                p.stop()
        if run.lease:
            run.lease.close()
            run.lease = None
        runtime.listeners.flush(5)
        return phone


__all__ = ["SmartBase", "COMPOSE", "Script", "item", "sent"]
