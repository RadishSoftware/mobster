import unittest

from mobile_agent import artifacts, result_blocks


class ComparisonProjectionTests(unittest.TestCase):
    def test_html_and_markdown_keep_fact_order_first_values_and_empty_cells(self):
        block = {"type": "comparison", "id": "compare", "options": [
            {"id": "a", "name": "A", "facts": [
                {"label": "Price|tier", "value": "<first>|one"},
                {"label": "Price|tier", "value": "duplicate"},
                {"label": "", "value": "empty label"},
            ]},
            {"id": "b", "name": "B", "facts": [
                {"label": "Later", "value": "last"},
                {"label": "Price|tier", "value": ""},
            ]},
            {"id": "c", "name": "C"},
        ]}
        run = {"summary": {"blocks": {"blocks": [block]}}}
        page = artifacts.document(run)
        body = page.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        self.assertEqual(body,
                         '<tr><th scope="row">Price|tier</th><td>&lt;first&gt;|one</td><td></td><td></td></tr>'
                         '<tr><th scope="row"></th><td>empty label</td><td></td><td></td></tr>'
                         '<tr><th scope="row">Later</th><td></td><td>last</td><td></td></tr>')
        self.assertEqual(result_blocks.block_markdown(block),
                         "| | A | B | C |\n| --- | --- | --- | --- |\n"
                         "| Price\\|tier | <first>\\|one |  |  |\n"
                         "|  | empty label |  |  |\n"
                         "| Later |  | last |  |\n")

    def test_table_comparison_preserves_formatted_cells_without_double_escaping(self):
        block = {"type": "table", "id": "table", "view": "compare", "columns": [
            {"key": "name", "label": "Name", "type": "text"},
            {"key": "site", "label": "Site & details", "type": "link"},
        ], "rows": [
            {"cells": {"name": "<A>", "site": "https://example.com/?a=1&b=2"}},
            {"cells": {"name": "B"}},
        ]}
        page = artifacts.document({"summary": {"blocks": {"blocks": [block]}}})
        self.assertIn('<th class="">&lt;A&gt;</th>', page)
        self.assertIn('<th scope="row">Site &amp; details</th>', page)
        self.assertIn('<td><a href="https://example.com/?a=1&amp;b=2">example.com</a></td><td></td>', page)
        self.assertNotIn("&amp;lt;", page)


if __name__ == "__main__":
    unittest.main()
