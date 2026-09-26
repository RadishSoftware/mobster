"""Pages a request names are opened directly: an address, a web search, a Wikipedia article by title. Offline."""

import unittest

from mobile_agent.agent import requested_article, requested_search

GO = "https://en.m.wikipedia.org/w/index.php?go=Go&search="


class RequestedArticleTests(unittest.TestCase):
    def test_an_article_named_by_its_title_opens_as_wikipedia_go_search(self):
        self.assertEqual(requested_article("Go to the Wikipedia article for the Eiffel Tower and find the year it "
                                           "was completed."), GO + "Eiffel+Tower")
        self.assertEqual(requested_article("Open the Wikipedia article about iPhone 15 Pro and find the year it was "
                                           "released. Do not change any setting."), GO + "iPhone+15+Pro")
        self.assertEqual(requested_article("Open the Wikipedia page on Leonardo da Vinci then report his birth year"),
                         GO + "Leonardo+da+Vinci")

    def test_a_described_article_is_a_link_to_follow_not_a_search(self):
        for goal in ("Open the Wikipedia article about the engineer the Eiffel Tower is named after, and report "
                     "the year he was born.",
                     "then in Safari open the Wikipedia article about that model and report the year it was released.",
                     "In Safari, find the year the Eiffel Tower was completed on its Wikipedia article"):
            self.assertIsNone(requested_article(goal), goal)

    def test_a_web_search_the_request_asks_for_is_not_replaced(self):
        goal = "Search the web for Hedy Lamarr, open her Wikipedia article, and report the year she was born."
        self.assertIsNone(requested_article(goal))
        self.assertIn("fulltext=1", requested_search(goal))


if __name__ == "__main__":
    unittest.main()
