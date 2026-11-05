"""The parity matrix (mobile_agent/parity.py): every command, `mobster test` mode, MCP tool and API route group is in a
row, everything a row names exists, a missing surface has a recorded reason or a ranked gap, and a testing command
with no MCP tool and no recorded reason fails. Offline: it reads the parsers, the route table and the tool list."""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import parity

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_docs_reference.py"


def load_generator():
    spec = importlib.util.spec_from_file_location("gen_docs_reference", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SCRIPT.is_file(), "the docs generator is not part of this copy")
class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        generator = load_generator()
        cls.cli, cls.api = generator.cli_paths(), generator.api_paths()
        cls.mcp = {tool["name"] for tool in generator.tool_list()[0]}

    def problems(self, cli=None, mcp=None, api=None):
        return parity.analyse(self.cli if cli is None else cli, self.api if api is None else api,
                              self.mcp if mcp is None else mcp)

    def test_the_rows_match_the_code(self):
        self.assertEqual(self.problems(), [])

    def test_the_devices_actions_and_pull_media_are_surfaces_and_their_rows_name_them(self):
        self.assertLessEqual({"devices lock-probe", "devices status"}, self.cli)
        self.assertIn("pull_media", self.mcp)
        by_name = {r.name: r for r in parity.ROWS}
        self.assertIn("devices lock-probe", by_name["Lock state"].cli)
        self.assertIn("devices status", by_name["Device status"].cli)
        self.assertIn("pull_media", by_name["Photos both ways"].mcp)
        self.assertNotIn("Mac app", " ".join(by_name["Display settings"].no.values()))

    def test_a_new_testing_command_with_no_row_fails_and_says_what_to_add(self):
        found = self.problems(cli=self.cli | {"test coverage"})
        self.assertEqual(len(found), 1)
        self.assertIn("`mobster test coverage` is in no row", found[0])
        self.assertIn("MCP surfaces, or the reason", found[0])

    def test_a_new_mcp_tool_or_api_group_with_no_row_fails(self):
        self.assertIn("The MCP tool brand_new is in no row", self.problems(mcp=self.mcp | {"brand_new"})[0])
        self.assertIn("/api/brandnew is in no row", self.problems(api=self.api | {"/api/brandnew/x"})[0])

    def test_a_testing_command_with_no_mcp_tool_and_no_reason_fails(self):
        row = parity.row("Coverage report", "Testing", ["test coverage"], ["tests"], [])
        rows = parity.ROWS + (row,)
        with mock.patch.object(parity, "ROWS", rows):
            found = self.problems(cli=self.cli | {"test coverage"})
        self.assertEqual(found, ["Coverage report: no mcp surface and no reason. Add one to `no` or a ranked `gap`."])

    def test_a_testing_command_may_leave_mcp_out_with_a_reason_but_not_as_an_open_gap(self):
        reasoned = parity.row("Coverage report", "Testing", ["test coverage"], ["tests"], [],
                              no={"mcp": "An agent reads the report file."})
        gapped = parity.row("Coverage report", "Testing", ["test coverage"], ["tests"], [],
                            gap={"mcp": (1, "Not built.")})
        for row, expected in ((reasoned, []), (gapped, ["Coverage report: a Testing row has no open MCP gap. Build it, "
                                                         "or write why not in `no`."])):
            with mock.patch.object(parity, "ROWS", parity.ROWS + (row,)):
                self.assertEqual(self.problems(cli=self.cli | {"test coverage"}), expected)

    def test_a_row_cannot_name_what_isnt_there_or_record_a_reason_for_a_surface_it_has(self):
        wrong = parity.row("Ghost", "Phone", ["ghost"], ["ghost"], ["ghost_tool"])
        both = parity.row("Both", "Phone", [], [], ["tap"], no={"mcp": "x"}, gap={"cli": (1, "x"), "api": (1, "x")})
        with mock.patch.object(parity, "ROWS", parity.ROWS + (wrong, both)):
            found = "\n".join(self.problems())
        for text in ("`mobster ghost` is not a command", "/api/ghost is not a route", "ghost_tool is not an MCP tool",
                     "mcp has surfaces and a recorded reason"):
            self.assertIn(text, found)

    def test_a_gap_needs_a_rank_and_a_sentence(self):
        bad = parity.row("Rankless", "Phone", [], [], ["tap"], no={"cli": "x"}, gap={"api": (9, "x")})
        with mock.patch.object(parity, "ROWS", parity.ROWS + (bad,)):
            self.assertIn("the api gap needs a rank 1 to 3", "\n".join(self.problems()))

    def test_the_gaps_come_ranked_with_the_testing_icps_first(self):
        ranks = [item[0] for item in parity.gaps()]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(ranks[0], 1)

    def test_the_testing_icps_essentials_have_an_mcp_tool(self):
        by_name = {r.name: r for r in parity.ROWS}
        for name, tool in (("Import Maestro flows as checks", "import_maestro"),
                           ("Read each check's history", "test_history"),
                           ("Overnight run: list it, run it now", "test_schedule"),
                           ("Run saved checks, with --locale, --a11y, --sim, --parallel", "run_tests")):
            self.assertIn(tool, by_name[name].mcp)
            self.assertIn(tool, self.mcp)


if __name__ == "__main__":
    unittest.main()
