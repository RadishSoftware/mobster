import threading
import time

from mobile_agent.server import make_handler
from mobile_agent.tests.seam_support import request
from mobile_agent.tests.seam_tasks import SmartBase
from mobile_agent.threads import service as threads_service


class ThreadsBase(SmartBase):
    def setUp(self):
        super().setUp()
        self.handlers = {}

    def call(self, runtime, method, path, body=None, *, headers=None, app_session=None):
        key = (id(runtime), app_session)
        if key not in self.handlers:
            self.handlers[key] = make_handler(runtime, app_session=app_session)
        return request(self.handlers[key], method, path, body, headers=headers)

    def service(self, runtime):
        return threads_service.service_for(runtime)

    def thread(self, runtime, text="Find my last message from Kate Bell", **headers):
        status, data, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": text}},
                                    headers=headers or None)
        self.assertEqual(status, 201, data)
        return data

    def settle(self, runtime):
        runtime.listeners.flush(5)

    def items(self, runtime, thread_id):
        status, data, _ = self.call(runtime, "GET", f"/api/threads/{thread_id}")
        self.assertEqual(status, 200, data)
        return data["items"]

    def ask(self, run, request_body, timeout=5):
        box = {}

        def target():
            box["answer"] = run.request_approval(request_body, timeout=timeout)
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (run.approval is not None and any(
                e["event"] == "approval_requested" and e["approval_id"] == run.approval["id"] for e in run.events)):
            time.sleep(.005)
        self.assertIsNotNone(run.approval)
        return thread, box


def clarify(question="Which Sam: Sam Lee or Sam Park?", choices=(("lee", "Sam Lee"), ("park", "Sam Park"))):
    return {"kind": "clarify", "operation": "ASK_USER", "label": question[:80], "question": question,
            "choices": [{"id": i, "label": label} for i, label in choices], "allow_text": True}


def commit(text="I'm running late"):
    return {"operation": "TAP", "label": "Send", "text": text, "kind": "commit", "title": "Send to Sam",
            "act": "send_message"}


def bus_events(runtime, topic, since):
    return [e for e in runtime.bus.since({topic}, since)]

