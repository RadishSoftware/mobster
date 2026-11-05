import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import artifacts, chart_svg, maps
from mobile_agent import result_blocks as rb
from mobile_agent.tests.test_server_engine import MESSAGES, Base

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000")


def fake_map_helper(folder, code=0, said="", places=None, missing=()):
    answer = json.dumps({"width": 720, "height": 320, "missing": list(missing),
                         "places": places if places is not None
                         else [{"id": "p1", "x": 100, "y": 80, "lat": 37.78, "lon": -122.4}]})
    path = Path(folder) / "map"
    calls = Path(folder) / "calls"
    path.write_text("#!/bin/sh\n"
                    f"echo x >> '{calls}'\n"
                    f"cp \"$1\" '{folder}/last.json'\n"
                    f"printf '%s' \"{said}\" >&2\n"
                    f"[ {code} -eq 0 ] || exit {code}\n"
                    f"printf '\\211PNG' > \"$2\"\n"
                    f"printf '%s\\n' '{answer}'\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


PLACES = [{"id": "p1", "name": "Blue Bottle Coffee", "address": "66 Mint St"},
          {"id": "p2", "name": "Pier 39", "lat": 37.8, "lon": -122.41}]


class MapTests(unittest.TestCase):
    def test_a_snapshot_is_drawn_once_then_kept(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as cache:
            helper = fake_map_helper(folder, missing=["p2"])
            first = maps.snapshot(PLACES, near="San Francisco, CA", dark=True, helper=helper, cache_dir=cache)
            self.assertEqual(first["image"], b"\x89PNG")
            self.assertEqual(first["places"][0]["id"], "p1")
            self.assertEqual(first["missing"], ["p2"])
            sent = json.loads((Path(folder) / "last.json").read_text())
            self.assertEqual(sent,
                             {"places": PLACES, "near": "San Francisco, CA", "width": 720, "height": 360, "dark": True})
            again = maps.snapshot(PLACES, near="San Francisco, CA", dark=True, helper=helper, cache_dir=cache)
            self.assertEqual(again, first)
            self.assertEqual((Path(folder) / "calls").read_text().count("x"), 1)  # the second came from disk
            maps.snapshot(PLACES, near="San Francisco, CA", dark=False, helper=helper, cache_dir=cache)
            self.assertEqual((Path(folder) / "calls").read_text().count("x"), 2)  # light is its own picture

    def test_a_failure_says_why(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as cache:
            helper = fake_map_helper(folder, code=1, said="Mobster couldn't find any of these places on the map")
            with self.assertRaisesRegex(maps.MapError, "^Mobster couldn't find any of these places on the map.$"):
                maps.snapshot(PLACES, helper=helper, cache_dir=cache)
            with self.assertRaisesRegex(maps.MapError, "no places"):
                maps.snapshot([], helper=helper, cache_dir=cache)

    def test_places_of_a_map_and_of_a_table(self):
        block = rb.validate_block(rb.map_block(PLACES + [{"name": "Note only", "address": "1 Main St", "note": "x"}]))
        self.assertEqual([p["name"] for p in maps.places_of(block)], ["Blue Bottle Coffee", "Pier 39", "Note only"])
        self.assertNotIn("note", maps.places_of(block)[2])
        table = rb.validate_block(rb.table(["Name", "Address", {"label": "Rating", "type": "number"}],
                                           [["Blue Bottle", "66 Mint St", 4.6], ["Nowhere", "", 3],
                                            ["Ritual", "1026 Valencia St", 4.7]]))
        self.assertEqual(maps.places_of(table), [{"id": "r0", "name": "Blue Bottle", "address": "66 Mint St"},
                                                 {"id": "r2", "name": "Ritual", "address": "1026 Valencia St"}])
        coords = rb.validate_block(rb.table(["Place", "Lat", "Lng"], [["Pier", "37.8", "-122.41"], ["Bad", "91", "0"]]))
        self.assertEqual(maps.places_of(coords), [{"id": "r0", "name": "Pier", "lat": 37.8, "lon": -122.41}])
        self.assertEqual(maps.places_of(rb.validate_block(rb.table(["Name", "Likes"], [["a", 1]]))), [])


class ChartTests(unittest.TestCase):
    def test_every_kind_draws_escaped_svg(self):
        labels = ["<b>Mon</b>", "Tue", "Wed"]
        for kind in ("bar", "line", "area", "pie", "sparkline"):
            svg = chart_svg.render({"kind": kind, "x": labels, "series": [{"label": "A & B", "values": [3, None, 5]},
                                                                          {"label": "C", "values": [1, 2, 3]}],
                                    "stacked": kind in ("bar", "area"), "title": "<x>"})
            self.assertTrue(svg.startswith('<svg class="chart-svg"'), kind)
            self.assertNotIn("<b>", svg)
            self.assertNotIn("<x>", svg)
        scatter = chart_svg.render({"kind": "scatter",
                                    "series": [{"label": "S", "points": [{"x": 1, "y": 2}, {"x": 3, "y": 1}]}],
                                    "xLabel": "Miles"})
        self.assertEqual(scatter.count("<circle"), 2)
        self.assertIn("Miles", scatter)
        many = chart_svg.render({"kind": "bar", "x": [f"Day {i}" for i in range(12)],
                                 "series": [{"label": "h", "values": list(range(12))}]})
        self.assertIn("Day 11", many)   # more than eight labels: rows, every label shown

    def test_a_tables_chart(self):
        table = rb.validate_block(rb.table([{"label": "Month", "type": "date"}, {"label": "Revenue", "type": "money"},
                                            "Note"],
                                           [["2026-01", 10, "x"], ["2026-02", 12, "y"]]) | {"view": "chart"})
        spec = chart_svg.from_table(table)
        self.assertEqual((spec["kind"], spec["x"], spec["format"]), ("line", ["Jan 2026", "Feb 2026"], "money"))
        self.assertEqual(spec["series"], [{"label": "Revenue", "values": [10, 12]}])
        self.assertIsNone(chart_svg.from_table(rb.validate_block(rb.table(["Name"], [["a"]]))))


RUN = {"id": "abcdef012345", "goal": "Plan my Saturday", "appName": "Maps", "finishedAt": 1791950000000,
       "summary": {"answer": "Your Saturday is set.", "blocks": rb.envelope([
           rb.chart(["Mon", "Tue"], [{"label": "Steps", "values": [3, 4]}], title="Steps", id="steps"),
           rb.map_block(PLACES, title="Coffee <near>", id="coffee"),
           rb.timeline([{"at": "9 AM", "title": "Leave", "state": "done"},
                        {"at": "1 PM", "title": "Lunch", "detail": "Tartine"}], id="day"),
           rb.comparison([{"name": "United", "facts": {"Price": "$320"}, "pros": ["Nonstop"]},
                          {"name": "Delta", "facts": {"Price": "$298"}, "cons": ["1 stop"]}], recommended="o1",
                         verdict="United.", id="flights"),
           rb.gallery([{"frameId": "f0000000001", "caption": "Home"}], id="screens"),
           rb.table(["Month", {"label": "Revenue", "type": "money"}], [["Jan", 1], ["Feb", 2]], title="Revenue",
                    id="rev") | {"view": "chart"},
       ])}}


class ReportTests(unittest.TestCase):
    def body(self, **options):
        return artifacts.document(RUN, frames={"f0000000001": bytes.fromhex("ffd8ffd9")}.get,
                                  **options).split("</style>", 1)[1]

    def test_a_pdf_has_a_cover_then_every_card(self):
        body = self.body(report=True, draw_map=lambda block: {"image": b"\x89PNG", "width": 720, "height": 320,
                                                              "places": [{"id": "p1", "x": 360, "y": 160}]})
        cover = body.split('</section>', 1)[0]
        self.assertIn('class="cover"', cover)
        self.assertIn("Plan my Saturday", cover)
        self.assertIn("Your Saturday is set.", cover)
        self.assertIn("In this report", cover)
        self.assertIn("Coffee &lt;near&gt;", cover)
        self.assertEqual(body.count('<svg class="chart-svg"'), 2)                      # the chart and the table's chart
        self.assertIn('<span class="pin" style="left:50.00%;top:50.00%">1</span>', body)
        self.assertIn("data:image/png;base64,", body)
        self.assertIn('<ol class="timeline">', body)
        self.assertIn('class="badge">Pick</span>', body)
        self.assertIn("+ Nonstop", body)
        self.assertIn('<div class="gallery"><figure><img src="data:image/jpeg;base64,', body)

    def test_without_a_map_the_places_are_still_listed(self):
        body = self.body(report=True, draw_map=lambda block: (_ for _ in ()).throw(maps.MapError("offline")))
        self.assertNotIn('class="map"', body)
        self.assertIn("66 Mint St", body)
        self.assertIn("37.80000, -122.41000", body)
        self.assertNotIn('class="cover"', self.body())
        self.assertNotIn("<img", self.body(screens=False).split('class="gallery"')[-1].split("</section>")[0])


class MapRouteTests(Base):
    def test_a_cards_map(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-000000000000"
        runtime = self.runtime()
        run = runtime.create(MESSAGES["id"], "Find coffee", "live")
        run.finish({"event": "result", "status": "completed", "answer": "Two cafés.",
                    "blocks": RUN["summary"]["blocks"]})
        if run.lease:
            run.lease.close()
            run.lease = None
        runtime.active_runs.clear()
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as data, \
                mock.patch("mobile_agent.paths.user_data_dir", return_value=data), \
                mock.patch("mobile_agent.native_helpers.find", return_value=fake_map_helper(folder)):
            status, body, _ = self.request(runtime, "GET",
                                           f"/api/runs/{run.id}/blocks/coffee/map?theme=dark&w=640&h=300")
            self.assertEqual(status, 200, body)
            self.assertTrue(body["image"].startswith("data:image/png;base64,"))
            self.assertEqual(body["places"][0]["id"], "p1")
            self.assertTrue(json.loads((Path(folder) / "last.json").read_text())["dark"])
            self.assertEqual(self.request(runtime, "GET", f"/api/runs/{run.id}/blocks/steps/map")[0], 404)
            self.assertEqual(self.request(runtime, "GET", f"/api/runs/{run.id}/blocks/coffee/map?w=wide")[0], 400)
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as data, \
                mock.patch("mobile_agent.paths.user_data_dir", return_value=data), \
                mock.patch("mobile_agent.native_helpers.find",
                           return_value=fake_map_helper(folder, code=1, said="Mobster couldn't draw the map")):
            status, body, _ = self.request(runtime, "GET", f"/api/runs/{run.id}/blocks/coffee/map")
            self.assertEqual((status, body["code"]), (503, "map_unavailable"))


if __name__ == "__main__":
    unittest.main()
