import unittest
from unittest import mock

from mobile_agent import devices
from mobile_agent.wda import identity as wda_identity
from mobile_agent.wda.identity import WrongDevice, verify

URL = "http://127.0.0.1:8100"


def reads(**info):
    return lambda url: info or None


class VerifyTests(unittest.TestCase):
    def test_the_matching_phone_proceeds(self):
        verify(URL, name="Michelle's iPhone", model="iPhone",
               read=reads(name="Michelle’s iPhone".replace("’", "'"), model="iPhone"))
        verify(URL, name="x", udid="AAAA", read=reads(name="other", udid="aaaa"))

    def test_another_phone_is_refused_by_name_or_udid(self):
        with self.assertRaisesRegex(WrongDevice, "not “Michelle's iPhone”"):
            verify(URL, name="Michelle's iPhone", read=reads(name="QA iPhone"))
        with self.assertRaises(WrongDevice):
            verify(URL, name="same", udid="AAAA", read=reads(name="same", udid="BBBB"))

    def test_an_unreadable_answer_refuses_and_nothing_known_reads_nothing(self):
        with self.assertRaisesRegex(WrongDevice, "couldn't confirm"):
            verify(URL, name="Michelle's iPhone", read=lambda url: None)
        verify(URL, read=mock.Mock(side_effect=AssertionError("must not read")))


class RuntimeRefusalTests(unittest.TestCase):
    def runtime_check(self, reported_name):
        from types import SimpleNamespace

        from mobile_agent import server
        phone = SimpleNamespace(primary=True, name="Michelle's iPhone", model=None)
        runtime = SimpleNamespace(manager=SimpleNamespace(settings=lambda: {}))
        with mock.patch.object(wda_identity, "reported", lambda url, timeout=3.0: {"name": reported_name}):
            server.Runtime.verify_phone(runtime, phone, URL)

    def test_a_run_on_the_wrong_phone_is_refused_with_409_and_the_right_one_proceeds(self):
        from mobile_agent.api_errors import APIError
        self.runtime_check("Michelle's iPhone")
        with self.assertRaises(APIError) as refused:
            self.runtime_check("QA iPhone")
        self.assertEqual((refused.exception.status, refused.exception.code), (409, "wrong_device"))


class SlotTests(unittest.TestCase):
    def test_the_first_slot_no_other_program_listens_on_is_chosen(self):
        self.assertEqual(devices.allocate_slot([], busy=lambda port: False), 1)
        busy = {8101, 9102}
        self.assertEqual(devices.allocate_slot([], busy=lambda port: port in busy), 3)


if __name__ == "__main__":
    unittest.main()
