"""Track memory: suggestions read out of what a person writes (SPEC §3.3 F3). A table of sentences and what each
suggests, if anything. Deterministic, offline."""

import unittest

from mobile_agent.memory.extract import Candidate, candidates

SUGGESTS = [
    ("Remember that my gym is the one on 5th Street", [Candidate("My gym is the one on 5th Street")]),
    ("remember my gym is the one on 5th Street.", [Candidate("My gym is the one on 5th Street")]),
    ("Please remember: my dentist is Dr. Lee on Valencia St. near the park.",
     [Candidate("My dentist is Dr. Lee on Valencia St. near the park")]),
    ("Can you remember that Kate Bell is my sister", [Candidate("Kate Bell is my sister")]),
    ("Don't forget that I'm vegetarian", [Candidate("I'm vegetarian")]),
    ("keep in mind I always fly from SFO", [Candidate("I always fly from SFO", pin=True)]),
    ("my sister's name is Kate Bell", [Candidate("My sister's name is Kate Bell")]),
    ("Hey, my favorite coffee is an oat flat white", [Candidate("My favorite coffee is an oat flat white")]),
    ("My kids are Sam and Alex", [Candidate("My kids are Sam and Alex")]),
    ("I always take the window seat", [Candidate("I always take the window seat", pin=True)]),
    ("I prefer short replies.", [Candidate("I prefer short replies", pin=True)]),
    ("I'd rather take the train", [Candidate("I'd rather take the train", pin=True)]),
    ("Call me Sam", [Candidate("Call me Sam", pin=True)]),
    ("Book a table for 2. I prefer quiet places. My partner is vegetarian.",
     [Candidate("I prefer quiet places", pin=True), Candidate("My partner is vegetarian")]),
    ("I prefer aisle seats. I never eat shellfish. My gym is on 5th.",       # at most two per message
     [Candidate("I prefer aisle seats", pin=True), Candidate("I never eat shellfish", pin=True)]),
]

SUGGESTS_NOTHING = [
    "",
    "Text Sam that I'm 10 minutes late",
    "What time does my gym close today?",
    "Is my package here yet",
    "My battery is low, can you check",
    "my order is late",
    "my coffee is cold",
    "my phone is at 20%",
    "Text Sam that my train is late",
    "Open Messages and tell her I always run late",
    "I never got your text",
    "Tell Kate to call me Monday",
    "call me when you are done",
    "Remember to buy milk",
    "Can you remember what my gym is called?",
    "Find my keys",
    "my PIN is 4821",
    "remember the gate code is 4821",
    "my wifi password is hunter2!",
    "Remember my card 4242 4242 4242 4242",
    "Remember x",                                   # too short to be worth asking
    "my problem is that it won't open",
]


class ExtractTests(unittest.TestCase):
    def test_what_each_sentence_suggests(self):
        for text, expected in SUGGESTS:
            with self.subTest(text=text):
                self.assertEqual(candidates(text), expected)

    def test_what_suggests_nothing(self):
        for text in SUGGESTS_NOTHING:
            with self.subTest(text=text):
                self.assertEqual(candidates(text), [])

    def test_a_long_message_full_of_abbreviations_stays_fast(self):
        # It runs on the listeners' one thread: 4,000 characters of "Dr. Dr. …" took half a second before.
        import time
        for text in ("Dr. " * 1000, "St. " * 990 + "Remember that my gym is on 5th"):
            started = time.perf_counter()
            candidates(text)
            self.assertLess(time.perf_counter() - started, 0.15)
        self.assertEqual(candidates("I saw Dr. Lee. My dentist is Dr. Lee on Valencia St. by the park"),
                         [Candidate("My dentist is Dr. Lee on Valencia St. by the park")])

    def test_odd_input(self):
        self.assertEqual(candidates(None), [])
        self.assertEqual(candidates(42), [])
        self.assertEqual(candidates("Remember that my gym is on 5th. " * 400)[:1], [Candidate("My gym is on 5th")])
        self.assertEqual(candidates("Remember that " + "x" * 400), [])          # longer than a fact may be


if __name__ == "__main__":
    unittest.main()
