"""A switch's observed 1/0 value is citable as on/off, and nothing else gains a state. Offline."""

import unittest

from mobile_agent.extraction import Evidence, validate_extraction
from mobile_agent.state import Element, Snapshot

SCHEMA = {"type": "object", "properties": {"airplane_mode": {"type": "string"}},
          "required": ["airplane_mode"], "additionalProperties": False}


def settings(value, role="Switch"):
    return Snapshot([Element("0", "Airplane Mode", role, (.1, .2, .8, .05), value=value),
                     Element("1", "Wi-Fi", "Cell", (.1, .3, .8, .05), value="Home")],
                    "Airplane Mode\nWi-Fi Home", 393, 852, "wda", bundle_id="com.apple.Preferences")


class SwitchEvidenceTests(unittest.TestCase):
    def evidence(self, snapshot):
        evidence = Evidence()
        evidence.add(snapshot, 0)
        return evidence.public()

    def test_an_off_switch_is_citable_as_off(self):
        evidence = self.evidence(settings("0"))
        state = next(e for e in evidence["entries"] if e["field"] == "state")
        self.assertEqual((state["text"], state["label_context"]), ("off", "Airplane Mode"))
        result = {"data": {"airplane_mode": "off"},
                  "citations": [{"path": "/airplane_mode", "evidence_id": state["id"], "quote": "off"}]}
        self.assertEqual(validate_extraction(result, SCHEMA, evidence)["data"], {"airplane_mode": "off"})

    def test_on_is_not_citable_from_an_off_switch(self):
        evidence = self.evidence(settings("0"))
        state = next(e for e in evidence["entries"] if e["field"] == "state")
        result = {"data": {"airplane_mode": "on"},
                  "citations": [{"path": "/airplane_mode", "evidence_id": state["id"], "quote": "on"}]}
        with self.assertRaises(ValueError):
            validate_extraction(result, SCHEMA, evidence)

    def test_only_switches_with_a_binary_value_gain_a_state(self):
        for snapshot in (settings("0", role="Cell"), settings("mixed"), settings("")):
            self.assertFalse([e for e in self.evidence(snapshot)["entries"] if e["field"] == "state"])
        self.assertEqual([e["text"] for e in self.evidence(settings("1"))["entries"] if e["field"] == "state"],
                         ["on"])


class RadioRowTests(unittest.TestCase):
    """Settings' Wi-Fi and Bluetooth rows have no switch; "Off" is the only off status."""

    def states(self, label, role="Button"):
        snapshot = Snapshot([Element("r", label, role, (.04, .3, .92, .06), locator="/row")], label, 393, 852,
                            "wda", bundle_id="com.apple.Preferences")
        evidence = Evidence()
        evidence.add(snapshot, 0)
        return [e["text"] for e in evidence.public()["entries"] if e["field"] == "state"]

    def test_a_connected_or_unconnected_radio_is_on_and_off_is_off(self):
        self.assertEqual(self.states("Wi-Fi, HomeNet_5G"), ["on"])
        self.assertEqual(self.states("Bluetooth, Not Connected"), ["on"])
        self.assertEqual(self.states("Wi-Fi, Off", role="Cell"), ["off"])
        self.assertEqual(self.states("Bluetooth, Off"), ["off"])

    def test_other_rows_gain_no_state(self):
        self.assertEqual(self.states("Wi-Fi"), [])
        self.assertEqual(self.states("Personal Hotspot, Off"), [])
        self.assertEqual(self.states("Wi-Fi, HomeNet_5G", role="StaticText"), [])


if __name__ == "__main__":
    unittest.main()
