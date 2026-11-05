"""Fake Instagram, Hinge and Yelp on sota_world's iPhone; every action lands in state["long_log"] with its time."""

import re
import time
from dataclasses import dataclass

from mobile_agent.tests import sota_world as world

INSTAGRAM, HINGE, YELP = "com.burbn.instagram", "co.hinge.mobile.ios", "com.yelp.yelpiphone"
MAPS, MESSAGES, HOME = world.MAPS, world.MESSAGES, world.HOME
LONG_APPS = {INSTAGRAM: "Instagram", HINGE: "Hinge", YELP: "Yelp"}
EXTRA_APPS = [{"id": name.casefold(), "name": name, "bundleId": bundle, "available": True, "installed": True,
               "availability": "demo", "automationVerified": False} for bundle, name in LONG_APPS.items()]

# Seconds each operation takes on a real iPhone over USB and on the Claude API, times SPEED["value"] (0 in unit tests,
# 1 in the bench). The scripted phone reads them through sota_world.LATENCY; the scripted models sleep their own.
# DESIGN.md 12.3's values, with Haiku refit on 8 Oct 2026: 57 real extraction calls on the fake frames took 1.9 s at the
# median (the long-form bench's extraction check), not 0.9 s.
PACE = {"ax": .35, "glide": .35, "tap": .25, "type": .6, "launch": 1.0, "home": .5, "drag": 2.0, "settle": .6,
        "sonnet": 2.5, "haiku": 1.9, "opus_synth": 20.0, "sonnet_synth": 8.0, "digest": 1.2, "contract": 1.5}
SPEED = {"value": 0.0}


def pace(on=True):
    """Turn the latency model on (the bench) or off (tests): the scripted phone and models wait these times."""
    SPEED["value"] = 1.0 if on else 0.0
    world.PACE["value"] = SPEED["value"]
    world.LATENCY.clear()
    if on:
        world.LATENCY.update({"observe": PACE["ax"], "SWIPE_UP": PACE["glide"], "SWIPE_DOWN": PACE["glide"],
                              "SWIPE_LEFT": PACE["glide"], "SWIPE_RIGHT": PACE["glide"], "TAP": PACE["tap"],
                              "TYPE": PACE["type"], "TYPE_SUBMIT": PACE["type"], "SET_TEXT": PACE["type"],
                              "LAUNCH_APP": PACE["launch"], "HOME": PACE["home"], "execute": PACE["tap"],
                              "tap_point": PACE["tap"], "drag": PACE["drag"]})


def sleep(kind):
    seconds = PACE.get(kind, 0) * SPEED["value"]
    if seconds > 0:
        time.sleep(seconds)


THEMES = {
    "footwork": ("footwork", "first step", "cone", "jab", "pivot", "euro step", "closeout", "drop step", "ladder"),
    "shooting": ("shooting", "shot", "elbow", "arc", "follow-through", "free throw", "release", "grip", "range"),
    "wnba": ("wnba",),
    "trades": ("trade", "deadline", "rumor", "cap space", "draft pick"),
    "conditioning": ("conditioning", "vertical", "jump", "recovery", "plyo", "sleep", "sprint", "mobility"),
    "analytics": ("analytics", "corner three", "shot chart", "efficiency", "points per", "mid-range", "three-point"),
}
THEME_NAMES = {"footwork": "Footwork drills", "shooting": "Shooting form", "wnba": "WNBA highlights",
               "trades": "Trade rumors", "conditioning": "Conditioning and vertical jump",
               "analytics": "Shot analytics"}

# (handle, theme, caption, audio, title card, key stat, call to action, claims, tags, quality)
_BASKETBALL = [
    ("hoopslab", "footwork", "My go-to first step drill before every practice #footwork", "Original audio",
     "3-CONE FIRST STEP", "4 sets x 30 s", "Save this for practice", ["Keep your hips low through the cones"],
     ["footwork", "drill"], 3),
    ("shotdoctor", "shooting", "Fix your elbow flare with one drill", "Original audio",
     "ELBOW UNDER THE BALL", "50 form shots from 3 feet", "Try it tonight", ["Line the elbow up under the ball"],
     ["shooting", "form"], 3),
    ("wnbaweekly", "wnba", "This rookie dropped 30 in her WNBA debut", "Highlights mix",
     "30 IN HER DEBUT", "Tamsin Hale: 30 pts, 6 ast", "Follow for nightly recaps", ["Hale scored 30 in her debut"],
     ["wnba", "highlights"], 2),
    ("deadlinedesk", "trades", "Trade deadline rumors: who is buying?", "Original audio",
     "DEADLINE BUYERS", "3 teams with cap space", "Comment your trade idea", ["Three teams have the cap space to buy"],
     ["trades", "rumors"], 2),
    ("verticalvault", "conditioning", "Add inches to your vertical in 6 weeks", "Workout beat",
     "VERTICAL IN 6 WEEKS", "3 plyo sessions a week", "Link in bio for the plan", ["Three plyometric sessions a week"],
     ["conditioning", "vertical"], 2),
    ("courtcharts", "analytics", "Why everyone shoots corner threes now", "Original audio",
     "CORNER THREE", "1.16 points per shot", "Share with your coach", ["Corner threes return 1.16 points per shot"],
     ["analytics", "three-point"], 3),
    ("courtvision", "footwork", "Beat a switch with this jab series", "Original audio",
     "JAB, JAB, GO", "Sell the jab for a full count", "Save it", ["A jab works only if you sell it"],
     ["footwork", "jab"], 3),
    ("swishmechanics", "shooting", "One-motion or two-motion shot? Here's the difference", "Original audio",
     "ONE-MOTION SHOT", "Release 0.1 s sooner", "Which do you shoot?", ["A one-motion shot releases sooner"],
     ["shooting", "release"], 3),
    ("courtsidequeens", "wnba", "Commissioner's Cup final recap", "Highlights mix",
     "CUP FINAL", "Won by 12 on the road", "Follow for recaps", ["The Cup final was won by 12"],
     ["wnba", "recap"], 2),
    ("tradewire", "trades", "The draft pick swap nobody is talking about", "Original audio",
     "PICK SWAP", "2 firsts and a swap", "Thoughts?", ["The rumored deal includes two first-round picks"],
     ["trades", "draft pick"], 2),
    ("recoveryroom", "conditioning", "Sleep is the best recovery tool you have", "Calm piano",
     "SLEEP = RECOVERY", "8+ hours on game days", "Save for later", ["Aim for eight hours on game days"],
     ["conditioning", "recovery"], 2),
    ("shotquality", "analytics", "The mid-range is not dead, here's the data", "Original audio",
     "MID-RANGE IS BACK", "0.98 points per shot for elite shooters", "Agree?",
     ["Elite mid-range shooters return 0.98 points per shot"], ["analytics", "mid-range"], 3),
    ("lowpostlab", "footwork", "Drop step like a big man", "Original audio",
     "DROP STEP", "Seal first, then spin baseline", "Try it in the post", ["Seal the defender before the drop step"],
     ["footwork", "post"], 2),
    ("arcangle", "shooting", "Aim for a 45 degree arc", "Original audio",
     "45° ARC", "Best entry angle: 45°", "Film your shot", ["A 45 degree arc gives the best entry angle"],
     ["shooting", "arc"], 3),
    ("hoopherstory", "wnba", "Three WNBA records that fell this season", "Highlights mix",
     "3 RECORDS FELL", "Most threes in a season: 129", "Follow for more", ["The single-season threes record is 129"],
     ["wnba", "records"], 2),
    ("capspacecast", "trades", "Why this contender won't trade its rookie", "Original audio",
     "KEEPING THE ROOKIE", "Rookie deal: 4 cheap years", "Agree or disagree?", ["A rookie deal gives four cheap years"],
     ["trades", "rumors"], 2),
    ("plyoprogram", "conditioning", "Box jumps are overrated, do these instead", "Workout beat",
     "DEPTH JUMPS", "3 x 5 depth jumps", "Save this workout", ["Depth jumps beat box jumps for reactivity"],
     ["conditioning", "plyometrics"], 2),
    ("efficiencyexpert", "analytics", "Free throws decide close games, the numbers", "Original audio",
     "FREE THROW RATE", "Top teams: 0.28 FT per shot", "Share it", ["Top teams draw 0.28 free throws per shot"],
     ["analytics", "free throws"], 2),
    ("splitstepsam", "footwork", "Two-foot jump stop vs one-two step", "Original audio",
     "JUMP STOP", "Two-foot stop: either foot can pivot", "Save it", ["A two-foot stop leaves both pivot feet"],
     ["footwork", "jump stop"], 2),
    ("followthrough", "shooting", "Hold your follow-through like this", "Original audio",
     "HOLD IT", "Hold the wrist for 1 second", "Try it", ["Hold the follow-through for a second"],
     ["shooting", "follow-through"], 2),
    ("wnbaweekly2", "wnba", "Rookie of the year race is wide open", "Highlights mix",
     "ROY RACE", "3 rookies within 2 ppg", "Who's your pick?", ["Three rookies are within two points per game"],
     ["wnba", "rookies"], 2),
    ("rumormill", "trades", "Sources: a star guard wants out", "Original audio",
     "STAR GUARD WANTS OUT", "Deadline: Feb 6", "Comment your team", ["The trade deadline is February 6"],
     ["trades", "deadline"], 1),
    ("sprintsquad", "conditioning", "Conditioning drill: 17s in under a minute", "Workout beat",
     "SEVENTEENS", "17 sideline sprints < 60 s", "Tag a teammate", ["Run 17 sideline sprints in under a minute"],
     ["conditioning", "sprints"], 2),
    ("chartroom", "analytics", "Shot chart of the league's best scorer", "Original audio",
     "SHOT CHART", "62% at the rim", "Save for film study", ["The best scorer hits 62 percent at the rim"],
     ["analytics", "shot chart"], 2),
    ("guardgym", "footwork", "Ladder drill for quicker feet", "Workout beat",
     "LADDER: 6 PATTERNS", "10 min a day for 3 weeks", "Save this", ["Ten minutes of ladder work a day"],
     ["footwork", "ladder"], 2),
    ("freethrowfix", "shooting", "A free throw routine that finally stuck", "Calm piano",
     "SAME ROUTINE", "3 dribbles, breathe, shoot", "Try it", ["Use the same routine every free throw"],
     ["shooting", "free throw"], 2),
    ("courtsidequeens2", "wnba", "Best defensive plays of the week", "Highlights mix",
     "TOP 5 STOPS", "Block of the week: 3 in a row", "Follow for more", ["The block of the week was three in a row"],
     ["wnba", "defense"], 1),
    ("tradewire2", "trades", "Grading the biggest trade so far", "Original audio",
     "TRADE GRADE: B+", "Both teams got younger", "Your grade?", ["The biggest trade so far grades a B+"],
     ["trades", "grades"], 2),
    ("mobilitycoach", "conditioning", "Ankle mobility for jumpers", "Calm piano",
     "ANKLE MOBILITY", "2 min before every session", "Save it", ["Two minutes of ankle mobility before sessions"],
     ["conditioning", "mobility"], 2),
    ("pacepace", "analytics", "Pace is up again this season", "Original audio",
     "PACE IS UP", "101.2 possessions a game", "Share with a teammate", ["Pace is 101.2 possessions a game"],
     ["analytics", "pace"], 2),
    ("eurostepdaily", "footwork", "The euro step, slowed down", "Original audio",
     "EURO STEP", "Long first step, short second", "Try it", ["Take a long first step and a short second"],
     ["footwork", "euro step"], 3),
    ("balanceshooter", "shooting", "Set your feet before the catch", "Original audio",
     "FEET BEFORE THE CATCH", "Hop into your shot", "Save it", ["Hop into the shot so the feet are set"],
     ["shooting", "balance"], 2),
    ("wnbalocker", "wnba", "Inside a WNBA travel day", "Vlog beat",
     "TRAVEL DAY", "4 cities in 7 days", "Follow the trip", ["Four cities in seven days"],
     ["wnba", "vlog"], 1),
    ("capology", "trades", "Cap space explained in 60 seconds", "Original audio",
     "CAP SPACE 101", "Space = cap - salaries", "Save it", ["Cap space is the cap minus salaries"],
     ["trades", "cap space"], 2),
    ("jumpcoachjo", "conditioning", "Why your vertical stopped improving", "Workout beat",
     "PLATEAU?", "Add 1 rest day a week", "Save it", ["Add a rest day when the vertical plateaus"],
     ["conditioning", "vertical"], 2),
    ("threeandd", "analytics", "The three-point revolution in one chart", "Original audio",
     "3PA PER GAME", "35 threes a game", "Share with a teammate", ["Teams take 35 threes a game"],
     ["analytics", "three-point"], 2),
    ("defenseonly", "footwork", "Closeout footwork: short choppy steps", "Original audio",
     "CLOSEOUT", "Last 3 steps short and choppy", "Try it", ["Chop the last three steps of a closeout"],
     ["footwork", "defense"], 3),
    ("gripcheck", "shooting", "Fingertips, not palm", "Original audio",
     "FINGERTIPS", "Light between palm and ball", "Check yours", ["Hold the ball on the fingertips"],
     ["shooting", "grip"], 2),
    ("wnbaweekly3", "wnba", "Trade deadline moves in the WNBA", "Highlights mix",
     "WNBA DEADLINE", "2 deals before the deadline", "Follow for more", ["Two WNBA deals happened before the deadline"],
     ["wnba", "trades"], 1),
    ("rumormill2", "trades", "Three-team trade framework leaked", "Original audio",
     "3-TEAM FRAMEWORK", "5 players, 2 picks", "Thoughts?", ["The leaked framework moves five players"],
     ["trades", "rumors"], 1),
    ("pivotpro", "footwork", "Reverse pivot to create space", "Original audio",
     "REVERSE PIVOT", "Chin the ball through the turn", "Save it", ["Chin the ball through a reverse pivot"],
     ["footwork", "pivot"], 2),
    ("rangecoach", "shooting", "Extend your range one foot at a time", "Original audio",
     "1 FOOT AT A TIME", "Move back after 7 of 10", "Try it", ["Move back only after making 7 of 10"],
     ["shooting", "range"], 3),
    ("hoopsrecovery", "conditioning", "Cold tub or no cold tub?", "Calm piano",
     "COLD TUB?", "10 min at 12°C", "Save it", ["Ten minutes at 12 degrees"],
     ["conditioning", "recovery"], 1),
    ("possessiongame", "analytics", "Offensive rebounds are back", "Original audio",
     "OFFENSIVE BOARDS", "28% rebound rate", "Share with a teammate", ["Offensive rebound rate is up to 28 percent"],
     ["analytics", "rebounding"], 2),
    ("ballhandlerz", "footwork", "Hesitation footwork that freezes defenders", "Original audio",
     "THE HESI", "Stop on the inside foot", "Try it", ["Stop on the inside foot for the hesitation"],
     ["footwork", "handles"], 2),
    ("shootaround", "shooting", "Shoot with a two-motion shot? Fine, here's why", "Original audio",
     "TWO-MOTION IS FINE", "More range, slower release", "Which camp are you?",
     ["A two-motion shot trades release speed for range"], ["shooting", "release"], 3),
]
_OFF_TOPIC = [
    ("pastanights", "cooking", "15-minute garlic noodles", "Kitchen beat", "GARLIC NOODLES", "15 min, 6 ingredients",
     "Save the recipe", ["Garlic noodles take 15 minutes"], ["cooking", "noodles"], 2),
    ("carryononly", "travel", "Pack 10 days in a carry-on", "Travel vlog", "10 DAYS, 1 BAG", "Roll, don't fold",
     "Save for your trip", ["Roll clothes to fit ten days"], ["travel", "packing"], 2),
    ("sheetpanfam", "cooking", "One-pan lemon chicken", "Kitchen beat", "ONE PAN", "40 min at 220°C", "Save it",
     ["One pan, forty minutes"], ["cooking", "chicken"], 1),
    ("lisbonlocal", "travel", "Three days in Lisbon", "Travel vlog", "LISBON IN 3 DAYS", "Day 2: Sintra",
     "Follow for more", ["Spend day two in Sintra"], ["travel", "lisbon"], 1),
    ("brothbyben", "cooking", "Shoyu broth from scratch", "Kitchen beat", "SHOYU BROTH", "6 hours, low heat",
     "Save it", ["Simmer six hours on low heat"], ["cooking", "ramen"], 2),
    ("cheapflightsclub", "travel", "The flight search trick nobody uses", "Travel vlog", "FLEXIBLE DATES",
     "Save up to 40%", "Follow for more", ["Flexible dates save up to 40 percent"], ["travel", "flights"], 1),
]
_ADS = [
    ("courtkicks", "The new Glide 3 is here", "Shop now", "GLIDE 3", "Now $89"),
    ("hydrafuel", "Hydration made for athletes", "Learn more", "HYDRAFUEL", "Zero sugar"),
    ("hoopcampus", "Summer skills camp, spots left", "Sign up", "SKILLS CAMP", "Ages 10-17"),
    ("snackbarco", "Protein bars that taste like dessert", "Order now", "SNACK BAR", "20 g protein"),
    ("streamplus", "Every game, live", "Install now", "STREAM+", "First month free"),
]
AD_POSITIONS = (7, 19, 28, 41, 53)
REPEATS = {24: 5, 36: 14, 48: 30}      # position -> the earlier position it shows again
OFF_TOPIC_POSITIONS = (11, 22, 33, 46, 50, 56)
FEED_LENGTH = 60


@dataclass(frozen=True)
class FeedItem:
    pos: int                   # 1-based position in the feed
    key: str                   # stable id of the content ("b03", "o2", "ad1"); a repeat shares its original's key
    kind: str                  # "reel" | "ad" | "repeat"
    handle: str
    caption: str
    audio: str
    theme: str | None          # a THEMES key, "cooking", "travel", or None for an ad
    title: str
    stat: str
    cta: str
    claims: tuple = ()
    tags: tuple = ()
    quality: int = 2
    relevant: bool = True

    @property
    def on_screen(self):
        return f"{self.title} / {self.stat} / {self.cta}"


def _build_feed():
    basketball, off_topic, ads = list(_BASKETBALL), list(_OFF_TOPIC), list(_ADS)
    feed, by_pos = [], {}
    for pos in range(1, FEED_LENGTH + 1):
        if pos in AD_POSITIONS:
            handle, caption, cta, title, stat = ads.pop(0)
            item = FeedItem(pos, f"ad{AD_POSITIONS.index(pos) + 1}", "ad", handle, caption, "Sponsored audio", None,
                            title, stat, cta, (), ("ad",), 1, False)
        elif pos in REPEATS:
            original = by_pos[REPEATS[pos]]
            item = FeedItem(pos, original.key, "repeat", original.handle, original.caption, original.audio,
                            original.theme, original.title, original.stat, original.cta, original.claims,
                            original.tags, original.quality, original.relevant)
        elif pos in OFF_TOPIC_POSITIONS:
            handle, theme, caption, audio, title, stat, cta, claims, tags, quality = off_topic.pop(0)
            item = FeedItem(pos, f"o{len(_OFF_TOPIC) - len(off_topic)}", "reel", handle, caption, audio, theme,
                            title, stat, cta, tuple(claims), tuple(tags), quality, False)
        else:
            handle, theme, caption, audio, title, stat, cta, claims, tags, quality = basketball.pop(0)
            item = FeedItem(pos, f"b{len(_BASKETBALL) - len(basketball):02d}", "reel", handle, caption, audio, theme,
                            title, stat, cta, tuple(claims), tuple(tags), quality, True)
        feed.append(item)
        by_pos[pos] = item
    assert not basketball and not off_topic and not ads, "the feed's layout and its content disagree"
    return tuple(feed)


FEED = _build_feed()
BY_HANDLE = {item.handle: item for item in FEED if item.kind != "repeat"}
EXTRA_ITEMS = {}          # a test's own feed items (new_long_state(feed=...)), drawn and extracted like FEED's


def item_by_handle(handle):
    handle = (handle or "").lstrip("@")
    return BY_HANDLE.get(handle) or EXTRA_ITEMS.get(handle)


def off_topic_feed(count=20):
    """A feed of ``count`` cooking videos (a feed that drifted off basketball)."""
    return tuple(FeedItem(i + 1, f"c{i}", "reel", f"cookclub{i}", f"Weeknight recipe number {i}: garlic noodles",
                          "Kitchen beat", "cooking", f"RECIPE {i}", f"{10 + i} minutes", "Save the recipe",
                          (f"Recipe {i} takes {10 + i} minutes",), ("cooking",), 2, False) for i in range(count))


PREFERENCES = "I'm into climbing and ramen, want kids someday, and live near Brooklyn."
SURVEY_REQUEST = "Go through my Hinge matches and find the most promising ones. " + PREFERENCES
RELIGION_REQUEST = SURVEY_REQUEST + " I'd prefer someone Catholic."
MY_INTERESTS = ("climbing", "ramen", "trail running", "jazz")
NEAR_BROOKLYN = ("brooklyn", "park slope", "williamsburg", "fort greene", "bushwick", "greenpoint", "crown heights",
                 "carroll gardens", "bed-stuy", "prospect heights")


@dataclass(frozen=True)
class Profile:
    name: str
    age: int
    location: str
    work: str
    kids: str                  # "Want someday" | "Open to children" | "Don't want children" | "Have children"
    interests: tuple
    prompts: tuple             # ((prompt, answer), ...) three of them
    state: str                 # "your_turn" | "their_turn" | "new_match"
    messages: tuple            # (("them" | "me", text), ...)
    questions: int = 0         # questions in their last message, unanswered
    religion: str | None = None


PROFILES = (
    Profile("Riley", 29, "Park Slope, Brooklyn", "Product designer", "Want someday", ("climbing", "ramen", "film"),
            (("A shower thought I recently had", "Every bouldering gym should serve ramen after 9 pm."),
             ("My simple pleasures", "Tonkotsu on a cold night and a new route at Cliffside Gowanus."),
             ("We'll get along if", "You'll belay me and let me pick the ramen spot.")),
            "your_turn", (("them", "Ok you have to tell me your favorite ramen spot. And do you climb outdoors too?"),),
            2),
    Profile("Noor", 31, "Williamsburg, Brooklyn", "Pediatric nurse", "Want someday", ("bouldering", "ramen", "pottery"),
            (("Typical Sunday", "Bouldering in the morning, a big bowl of shoyu ramen after."),
             ("I'm looking for", "Someone who wants a family someday and loves a slow Sunday."),
             ("Green flags I look for", "You text back and you tip well.")),
            "your_turn", (("me", "Your pottery is great"), ("them", "Thank you! What's your go-to climbing gym?")),
            1),
    Profile("Avery", 30, "Fort Greene, Brooklyn", "Data analyst", "Open to children", ("climbing", "ramen", "running"),
            (("I geek out on", "Ranking every ramen shop in the city in a spreadsheet."),
             ("Best travel story", "Sport climbing in Kalymnos for two weeks."),
             ("Together, we could", "Train for a half marathon.")),
            "their_turn", (("them", "Ramen spreadsheet is real, I'll share it"), ("me", "Please do!")), 0),
    Profile("Jules", 28, "Bushwick, Brooklyn", "Chef", "Don't want children", ("climbing", "ramen", "cooking"),
            (("My most irrational fear", "Running out of chili oil."),
             ("I'm weirdly attracted to", "People who can make a perfect soft egg."),
             ("Dating me is like", "A tasting menu: lots of small surprises.")),
            "your_turn", (("them", "Come by the restaurant sometime?"),), 1),
    Profile("Marisol", 32, "Astoria, Queens", "Architect", "Want someday", ("climbing", "ramen", "sketching"),
            (("A life goal of mine", "Climb El Capitan before I'm 40."),
             ("My simple pleasures", "Ramen in Queens, the best in the city, I'll fight you."),
             ("I'm looking for", "A partner for adventures and, someday, a family.")),
            "their_turn", (("them", "Hi!"), ("me", "Hey Marisol, how's your week?")), 0),
    Profile("Priya", 27, "Carroll Gardens, Brooklyn", "Lawyer", "Want someday", ("ramen", "tennis", "podcasts"),
            (("Typical Sunday", "Tennis, then ramen, then a nap."),
             ("I'm looking for", "Someone kind who wants kids eventually."),
             ("Unusual skills", "I can parallel park anything.")),
            "new_match", (), 0),
    Profile("Dana", 34, "Jersey City", "Teacher", "Have children", ("hiking", "baking"),
            (("My simple pleasures", "Sourdough on Saturday."),
             ("I'm looking for", "Someone great with kids, I have a 6-year-old."),
             ("Typical Sunday", "Farmers market and a long hike.")),
            "their_turn", (("me", "Your sourdough looks amazing"),), 0),
    Profile("Theo", 33, "Hoboken", "Engineer", "Open to children", ("climbing", "sailing"),
            (("Best travel story", "Sailed from Maine to Nova Scotia."),
             ("I geek out on", "Knots. Seriously."),
             ("Together, we could", "Learn to sail on the Hudson.")),
            "their_turn", (("me", "Teach me knots?"),), 0),
    Profile("Kai", 26, "Greenpoint, Brooklyn", "Musician", "Don't want children", ("jazz", "ramen"),
            (("I'm weirdly attracted to", "Anyone who knows what a ii-V-I is."),
             ("My simple pleasures", "Late-night ramen after a gig."),
             ("Dating me is like", "A jazz solo: you never know where it goes.")),
            "your_turn", (("them", "Come to my show Friday?"),), 1),
    Profile("Imani", 30, "Crown Heights, Brooklyn", "Doctor", "Want someday", ("running", "church choir", "travel"),
            (("Typical Sunday", "Choir in the morning, brunch after."),
             ("I'm looking for", "Someone who shares my faith and wants a family."),
             ("Best travel story", "Got lost in Lisbon and found the best pastel de nata.")),
            "their_turn", (("me", "Lisbon is on my list"),), 0, "Catholic"),
    Profile("Lena", 35, "Upper West Side, Manhattan", "Editor", "Don't want children", ("books", "opera"),
            (("My simple pleasures", "A new hardcover and a quiet café."),
             ("I geek out on", "Typography."),
             ("Typical Sunday", "Matinee at the opera.")),
            "new_match", (), 0),
    Profile("Owen", 29, "Bed-Stuy, Brooklyn", "Carpenter", "Want someday", ("woodworking", "cycling"),
            (("Unusual skills", "I built my own bed frame."),
             ("Typical Sunday", "Long ride to Coney Island."),
             ("I'm looking for", "A teammate for the long haul.")),
            "their_turn", (("me", "That bed frame is impressive"),), 0),
    Profile("Rosa", 31, "Prospect Heights, Brooklyn", "Social worker", "Want someday", ("climbing", "salsa"),
            (("My simple pleasures", "Salsa night on Thursdays."),
             ("A shower thought I recently had", "Climbing is just vertical dancing."),
             ("I'm looking for", "Someone to dance with and, one day, raise kids with.")),
            "your_turn", (("them", "Do you dance at all?"),), 1),
    Profile("Ezra", 36, "Harlem, Manhattan", "Professor", "Have children", ("chess", "ramen"),
            (("I geek out on", "Openings in chess."),
             ("My simple pleasures", "A quiet bowl of miso ramen."),
             ("Typical Sunday", "Chess in the park with my daughter.")),
            "their_turn", (("me", "Fellow chess fan!"),), 0),
    Profile("Mika", 27, "Long Island City, Queens", "Photographer", "Open to children", ("photography", "hiking"),
            (("Best travel story", "Shot the northern lights in Iceland."),
             ("Together, we could", "Find the best sunset spot in the city."),
             ("My simple pleasures", "Film cameras.")),
            "new_match", (), 0),
)
PROFILE_BY_NAME = {p.name: p for p in PROFILES}
DISCOVER = Profile("Jordan", 31, "Gowanus, Brooklyn", "Florist", "Want someday", ("climbing",),
                   (("My simple pleasures", "Fresh peonies."), ("Typical Sunday", "Climbing, then flowers."),
                    ("I'm looking for", "Someone warm.")), "new_match", (), 0)
# The top 3 for PREFERENCES: climbing, ramen, kids someday, near Brooklyn; then whose move it is.
GROUND_TRUTH = ("Riley", "Noor", "Avery")
# What a good shortlist says about the matches (any one word of each group, with the name, in the report).
PLANTED_FACTS = (
    ("riley_climbs_ramen", "Riley", ("climb", "boulder", "belay")),
    ("riley_asked", "Riley", ("asked", "question", "your turn", "reply")),
    ("noor_kids", "Noor", ("family", "kids", "children", "someday")),
    ("noor_asked", "Noor", ("asked", "question", "your turn", "reply", "gym")),
    ("avery_ramen", "Avery", ("ramen", "spreadsheet")),
    ("jules_no_kids", "Jules", ("kids", "children")),
    ("marisol_queens", "Marisol", ("queens", "astoria")),
    ("priya_no_climbing", "Priya", ("climb", "tennis")),
)


def profile_score(profile, *, preferences=("climbing", "ramen", "kids", "brooklyn")):
    """How well ``profile`` fits the stated preferences (GROUND_TRUTH's rule): one point each, then whose turn."""
    text = " ".join([*profile.interests, *(a for _, a in profile.prompts)]).casefold()
    score = 0.0
    if "climbing" in preferences and any(word in text for word in ("climb", "boulder")):
        score += 1
    if "ramen" in preferences and "ramen" in text:
        score += 1
    if "kids" in preferences and profile.kids in ("Want someday", "Open to children"):
        score += 1 if profile.kids == "Want someday" else .8
    if "brooklyn" in preferences and any(place in profile.location.casefold() for place in NEAR_BROOKLYN):
        score += 1
    score += {"your_turn": .3, "their_turn": .1, "new_match": 0}[profile.state] + .05 * profile.questions
    return round(score, 3)


CHAIN_REQUEST = "Find the best ramen near me across Maps and Yelp, then book Friday at 8"


@dataclass(frozen=True)
class Place:
    name: str
    address: str
    distance: str
    maps: float
    maps_reviews: int
    yelp: float
    yelp_reviews: int
    friday_times: tuple


RAMEN = (
    Place("Kumo Ramen House", "464 W 51st St", "0.4 mi", 4.6, 2100, 4.3, 1830, ("7:00 PM", "9:30 PM")),
    Place("Hikari Noodle Bar", "212 E 9th St", "0.9 mi", 4.8, 640, 4.7, 912, ("7:30 PM", "8:00 PM", "8:30 PM")),
    Place("Menya Sora", "88 Orchard St", "1.2 mi", 4.7, 310, 4.1, 205, ("8:00 PM",)),
    Place("Daikoku Broth Co.", "31 Bowery", "1.5 mi", 4.3, 980, 4.5, 1240, ("8:00 PM", "8:30 PM")),
    Place("Shio and Smoke", "140 Ludlow St", "0.7 mi", 4.5, 450, 4.6, 388, ("9:30 PM",)),
)
PLACE_BY_NAME = {p.name: p for p in RAMEN}
BEST_RAMEN = "Hikari Noodle Bar"
DAYS = ("Thu", "Fri", "Sat")


def new_long_state(**options):
    """A scripted phone's state with the long-form apps: sota_world's apps plus Instagram, Hinge and Yelp.
    ``options``: feed=FEED (another feed for a test), alert_at (an Alert after that many swipes), leave_at (another app
    in front after that many swipes), stuck (the feed won't advance), start_at (the first reel shown)."""
    state = world.new_phone_state()
    for item in options.get("feed") or ():
        if item.handle not in BY_HANDLE:
            EXTRA_ITEMS[item.handle] = item
    state.update({
        "long_log": [],
        "ig": {"stage": "home", "index": 0, "query": "", "tab": "Top", "alert": None, "swipes": 0,
               "feed": tuple(options.get("feed") or FEED), "alert_at": options.get("alert_at"),
               "leave_at": options.get("leave_at"), "stuck": bool(options.get("stuck")),
               "start_at": int(options.get("start_at") or 0), "liked": []},
        "hinge": {"stage": "discover", "scroll": 0, "who": None, "page": 0, "menu": False, "typed": "", "sent": [],
                  "liked": []},
        "yelp": {"stage": "home", "query": "", "place": None, "day": None, "time": None, "booked": None},
        "maps_ramen": None,
    })
    return state


def owns(state):
    app = state.get("app")
    return app in LONG_APPS or (app == MAPS and state.get("maps_ramen") is not None)


def takes(state, operation, target, text=None):
    """True when the long-form world handles this action (anything in its apps, a ramen search in Maps). Launching an
    app and Home stay sota_world's: they only set the app in front."""
    if "long_log" not in state or operation in ("LAUNCH_APP", "HOME"):
        return False
    if (state.get("app") == MAPS and operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT")
            and "ramen" in (text or "").casefold()):
        return True
    return owns(state)


def el(index, label, role, rect, **extra):
    return world.el(index, label, role, rect, **extra)


def tab_bar(labels, start=50):
    width = 1 / len(labels)
    return [el(start + i, label, "Button", (i * width, .93, width, .05)) for i, label in enumerate(labels)]


def screen(phone):
    from mobile_agent.state import Snapshot
    s = phone.state
    app = s["app"]
    if app == INSTAGRAM:
        rows, stage = instagram_rows(s["ig"]), s["ig"]["stage"]
    elif app == HINGE:
        rows, stage = hinge_rows(s["hinge"]), s["hinge"]["stage"]
    elif app == YELP:
        rows, stage = yelp_rows(s["yelp"]), s["yelp"]["stage"]
    else:
        rows, stage = maps_rows(s), "ramen"
    text = "\n".join(e.label for e in rows)
    return Snapshot(rows, text, 390, 844, world.SOURCE, bundle_id=app, revision=f"{app}-{stage}-{s['revision']}")


def current_reel(ig):
    feed = ig["feed"]
    return feed[min(max(ig["index"], 0), len(feed) - 1)]


def instagram_rows(ig):
    stage = ig["stage"]
    rows = []
    if stage == "home":
        rows = [el(0, "Instagram", "NavigationBar", (0, .05, 1, .07)),
                el(1, "Stories", "Other", (0, .13, 1, .1)),
                el(2, "Post by @weekendbakes", "Cell", (0, .24, 1, .55)),
                el(3, "Liked by 1,204 others", "StaticText", (.04, .8, .6, .03))]
        rows += tab_bar(("Home", "Search", "Reels", "Profile"))
    elif stage == "search":
        rows = [el(0, "Search", "SearchField", (.04, .07, .92, .05), editable=True, actions=("TAP", "TYPE"))]
        rows += [el(1 + i, label, "Cell", (0, .15 + .09 * i, 1, .08))
                 for i, label in enumerate(("Explore: travel", "Explore: food", "Explore: sports"))]
        rows += tab_bar(("Home", "Search", "Reels", "Profile"))
    elif stage == "results":
        rows = [el(0, "Search", "SearchField", (.04, .07, .92, .05), editable=True, value=ig["query"],
                   actions=("TAP", "TYPE"))]
        for i, tab in enumerate(("Top", "Reels", "Accounts", "Audio", "Tags", "Places")):
            rows.append(el(1 + i, tab, "Button", (.02 + .16 * i, .13, .15, .04), selected=ig["tab"] == tab))
        if ig["tab"] == "Reels":
            for i, item in enumerate(ig["feed"][:9]):
                rows.append(el(10 + i, f"Reel by @{item.handle}", "Cell",
                               (.005 + (i % 3) * .333, .19 + (i // 3) * .24, .325, .235)))
        else:
            rows += [el(10, f"#{ig['query'] or 'basketball'}", "Cell", (0, .19, 1, .07)),
                     el(11, "@hoopslab", "Cell", (0, .27, 1, .07)),
                     el(12, "Post by @courtvision", "Cell", (0, .35, 1, .4))]
        rows += tab_bar(("Home", "Search", "Reels", "Profile"))
    elif stage == "reel":
        item = current_reel(ig)
        rows = [el(0, f"Reel by @{item.handle}", "Cell", (0, 0, 1, 1)),
                el(1, "Back", "Button", (.02, .06, .1, .04)),
                el(2, "Reels", "StaticText", (.4, .06, .2, .04))]
        if item.kind == "ad":
            rows += [el(3, "Sponsored", "StaticText", (.04, .74, .25, .02)),
                     el(4, item.cta, "Button", (.04, .69, .92, .045))]
        rows += [el(5, f"@{item.handle}", "Button", (.04, .77, .4, .03)),
                 el(6, "Follow", "Button", (.46, .77, .16, .03)),
                 el(7, item.caption, "StaticText", (.04, .81, .74, .05)),
                 el(8, f"♫ {item.audio}", "StaticText", (.04, .87, .6, .025)),
                 el(9, "Like", "Button", (.86, .5, .1, .05)),
                 el(10, "Comment", "Button", (.86, .57, .1, .05)),
                 el(11, "Share", "Button", (.86, .64, .1, .05)),
                 el(12, "Save", "Button", (.86, .71, .1, .05)),
                 el(13, "More", "Button", (.86, .78, .1, .04))]
        if ig["alert"]:
            rows += [el(20, ig["alert"], "Alert", (.1, .35, .8, .25)),
                     el(21, "Not Now", "Button", (.12, .52, .37, .06)),
                     el(22, "Turn On", "Button", (.51, .52, .37, .06))]
    return rows


def instagram_execute(state, operation, label, text, tab=False):
    """``tab``: the target is in the tab bar (the results' "Reels" filter has the same label as the Reels tab)."""
    ig = state["ig"]
    stage = ig["stage"]
    if operation == "LAUNCH_APP":
        state["app"] = INSTAGRAM
        return
    if stage == "reel" and ig["alert"]:
        if operation == "TAP" and label in ("Not Now", "Turn On"):
            ig["alert"] = None
        return  # the alert takes every touch
    if operation == "TAP" and label in ("Home", "Search", "Reels", "Profile") and stage != "reel" and (
            tab or stage != "results" or label != "Reels"):
        if label == "Search":
            ig["stage"] = "search"
        elif label == "Reels":
            ig["stage"], ig["index"] = "reel", ig["start_at"]
        else:
            ig["stage"] = "home"
        return
    if stage in ("search", "results") and operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT") and label == "Search":
        ig["query"] = (text or "") if operation == "SET_TEXT" else (ig["query"] + (text or "") if stage == "results"
                                                                   else text or "")
        if operation == "TYPE_SUBMIT" or stage == "results":
            ig["stage"], ig["tab"] = "results", "Top"
        else:
            ig["stage"] = "results"
        return
    if stage == "results" and operation == "TAP":
        if label in ("Top", "Reels", "Accounts", "Audio", "Tags", "Places"):
            ig["tab"] = label
        elif label.startswith("Reel by @"):
            handle = label.removeprefix("Reel by @")
            index = next((i for i, item in enumerate(ig["feed"][:9]) if item.handle == handle), 0)
            ig["stage"], ig["index"] = "reel", index
        return
    if stage == "reel":
        if operation == "SWIPE_UP" and not ig["stuck"]:
            ig["index"] = min(ig["index"] + 1, len(ig["feed"]) - 1)
            ig["swipes"] += 1
            if ig["alert_at"] and ig["swipes"] == ig["alert_at"]:
                ig["alert"] = "Turn on notifications? Get notified when creators you follow go live."
            if ig["leave_at"] and ig["swipes"] == ig["leave_at"]:
                state["app"], state["stage"], state["typed"] = MESSAGES, "inbox", ""
        elif operation == "SWIPE_DOWN":
            ig["index"] = max(ig["index"] - 1, 0)
        elif operation == "TAP" and label == "Back":
            ig["stage"] = "results"
        elif operation == "TAP" and label in ("Like", "Save", "Follow"):
            ig["liked"].append((label, current_reel(ig).handle))


HINGE_TABS = ("Discover", "Standouts", "Likes You", "Matches", "My Profile")
MATCH_ROWS = 9            # rows on one screen of Matches
MATCH_SCROLL = 6          # rows one swipe moves
STATE_LABELS = {"your_turn": "Your turn", "their_turn": "Their turn", "new_match": "New match"}


def last_message(profile):
    if not profile.messages:
        return "Start the conversation"
    who, text = profile.messages[-1]
    return text if who == "them" else f"You: {text}"


def hinge_rows(h):
    stage = h["stage"]
    rows = []
    if stage == "discover":
        p = DISCOVER
        rows = [el(0, "Discover", "StaticText", (.04, .06, .5, .04)),
                el(1, f"{p.name}, {p.age}", "StaticText", (.04, .12, .6, .04)),
                el(2, "Photo 1 of 6", "Image", (.04, .17, .92, .45)),
                el(3, p.prompts[0][0], "StaticText", (.06, .64, .88, .03)),
                el(4, p.prompts[0][1], "StaticText", (.06, .68, .88, .04)),
                el(5, "Skip", "Button", (.06, .8, .16, .07)),
                el(6, "Like", "Button", (.78, .8, .16, .07))]
        rows += tab_bar(HINGE_TABS)
    elif stage == "matches":
        rows = [el(0, "Matches", "NavigationBar", (0, .05, 1, .08))]
        top = h["scroll"]
        for k, profile in enumerate(PROFILES[top:top + MATCH_ROWS]):
            y = .15 + .085 * k
            rows += [el(10 + 3 * k, profile.name, "Cell", (0, y, 1, .08)),
                     el(11 + 3 * k, last_message(profile), "StaticText", (.2, y + .04, .5, .025)),
                     el(12 + 3 * k, STATE_LABELS[profile.state], "StaticText", (.74, y + .01, .22, .025))]
        rows += tab_bar(HINGE_TABS, start=80)
    elif stage == "chat":
        p = PROFILE_BY_NAME[h["who"]]
        rows = [el(0, p.name, "NavigationBar", (0, .05, 1, .08)),
                el(1, "Back", "Button", (.02, .06, .12, .04)),
                el(2, "Profile", "Button", (.8, .06, .18, .04))]
        for i, (who, text) in enumerate(p.messages):
            rows.append(el(3 + i, text, "StaticText", ((.3 if who == "me" else .04), .18 + .08 * i, .66, .06)))
        if not p.messages:
            rows.append(el(3, f"You matched with {p.name}. Say hi!", "StaticText", (.1, .4, .8, .04)))
        rows.append(el(20, "Send a message", "TextField", (.04, .9, .74, .045), editable=True, value=h["typed"],
                       actions=("TAP", "TYPE")))
        if h["typed"]:
            rows.append(el(21, "Send", "Button", (.82, .9, .14, .045)))
    elif stage == "profile":
        p = PROFILE_BY_NAME[h["who"]]
        rows = [el(0, "Back", "Button", (.02, .06, .12, .04)),
                el(1, f"{p.name}, {p.age}", "StaticText", (.18, .06, .5, .04)),
                el(2, "More", "Button", (.86, .06, .12, .04))]
        if h["page"] == 0:
            rows += [el(3, "Photo 1 of 6", "Image", (.04, .12, .92, .4)),
                     el(4, p.prompts[0][0], "StaticText", (.06, .54, .7, .03)),
                     el(5, p.prompts[0][1], "StaticText", (.06, .58, .7, .05)),
                     el(6, "Like prompt", "Button", (.82, .56, .14, .05)),
                     el(7, f"Age {p.age}", "StaticText", (.06, .66, .4, .03)),
                     el(8, p.location, "StaticText", (.06, .7, .6, .03)),
                     el(9, p.work, "StaticText", (.06, .74, .6, .03)),
                     el(10, f"Children: {p.kids}", "StaticText", (.06, .78, .6, .03))]
            if p.religion:
                rows.append(el(11, f"Religion: {p.religion}", "StaticText", (.06, .82, .6, .03)))
        else:
            rows += [el(3, "Photo 2 of 6", "Image", (.04, .1, .92, .3)),
                     el(4, p.prompts[1][0], "StaticText", (.06, .42, .7, .03)),
                     el(5, p.prompts[1][1], "StaticText", (.06, .46, .7, .05)),
                     el(6, "Like prompt", "Button", (.82, .44, .14, .05)),
                     el(7, "Photo 3 of 6", "Image", (.04, .53, .92, .2)),
                     el(8, p.prompts[2][0], "StaticText", (.06, .75, .7, .03)),
                     el(9, p.prompts[2][1], "StaticText", (.06, .79, .7, .05)),
                     el(10, "Like prompt", "Button", (.82, .77, .14, .05)),
                     el(11, "Interests: " + ", ".join(p.interests), "StaticText", (.06, .86, .88, .03))]
        if h["menu"]:
            rows += [el(30, "Options", "Sheet", (0, .7, 1, .3)),
                     el(31, "Unmatch", "Button", (.04, .74, .92, .05)),
                     el(32, "Report", "Button", (.04, .8, .92, .05)),
                     el(33, "Cancel", "Button", (.04, .88, .92, .05))]
    elif stage == "me":
        rows = [el(0, "My Profile", "NavigationBar", (0, .05, 1, .08)),
                el(1, "My interests: " + ", ".join(MY_INTERESTS), "StaticText", (.04, .16, .92, .04)),
                el(2, "Edit profile", "Button", (.04, .22, .4, .04))]
        rows += tab_bar(HINGE_TABS)
    return rows


def hinge_execute(state, operation, label, text):
    h = state["hinge"]
    stage = h["stage"]
    if operation == "LAUNCH_APP":
        state["app"] = HINGE
        return
    if operation == "TAP" and label in HINGE_TABS and stage in ("discover", "matches", "me"):
        h["stage"] = {"Discover": "discover", "Matches": "matches", "My Profile": "me"}.get(label, "discover")
        h["menu"] = False
        return
    if stage == "discover" and operation in ("TAP", "SWIPE_LEFT", "SWIPE_RIGHT"):
        h["liked"].append((operation, label or "", DISCOVER.name))      # Skip, Like or a deck swipe: a person action
        return
    if stage == "matches":
        if operation == "SWIPE_UP":
            h["scroll"] = min(h["scroll"] + MATCH_SCROLL, max(0, len(PROFILES) - MATCH_ROWS))
        elif operation == "SWIPE_DOWN":
            h["scroll"] = max(h["scroll"] - MATCH_SCROLL, 0)
        elif operation == "TAP" and label in PROFILE_BY_NAME:
            h["stage"], h["who"], h["typed"] = "chat", label, ""
        return
    if stage == "chat":
        if operation == "TAP" and label == "Back":
            h["stage"], h["typed"] = "matches", ""
        elif operation == "TAP" and label == "Profile":
            h["stage"], h["page"], h["menu"] = "profile", 0, False
        elif operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT") and label == "Send a message":
            h["typed"] = (text or "") if operation == "SET_TEXT" else h["typed"] + (text or "")
            if operation == "TYPE_SUBMIT" and h["typed"]:
                h["sent"].append((h["who"], h["typed"]))
                h["typed"] = ""
        elif operation == "TAP" and label == "Send" and h["typed"]:
            h["sent"].append((h["who"], h["typed"]))
            h["typed"] = ""
        return
    if stage == "profile":
        if h["menu"]:
            if operation == "TAP" and label in ("Unmatch", "Report"):
                h["liked"].append((operation, label, h["who"]))
            if operation == "TAP" and label in ("Unmatch", "Report", "Cancel"):
                h["menu"] = False
            return
        if operation == "SWIPE_UP":
            h["page"] = 1
        elif operation == "SWIPE_DOWN":
            h["page"] = 0
        elif operation == "TAP" and label == "Back":
            h["stage"], h["page"] = "chat", 0
        elif operation == "TAP" and label == "More":
            h["menu"] = True
        elif operation == "TAP" and label == "Like prompt":
            h["liked"].append((operation, label, h["who"]))
        return


def maps_rows(state):
    query = state.get("maps_ramen") or "ramen"
    rows = [el(0, query, "SearchField", (.04, .1, .92, .05), editable=True, value=query, actions=("TAP", "TYPE"))]
    for i, place in enumerate(RAMEN):
        y = .18 + .14 * i
        rows += [el(1 + 3 * i, place.name, "Cell", (0, y, 1, .06)),
                 el(2 + 3 * i,
                    f"{place.maps} ★ ({place.maps_reviews:,}) · Ramen · {place.distance}".replace(" · ", ", "),
                    "StaticText", (.05, y + .065, .9, .03)),
                 el(3 + 3 * i, place.address, "StaticText", (.05, y + .1, .6, .03))]
    return rows


def yelp_rows(y):
    stage = y["stage"]
    rows = []
    if stage == "home":
        rows = [el(0, "Search for ramen, tacos, plumbers", "SearchField", (.04, .08, .92, .05), editable=True,
                   actions=("TAP", "TYPE")),
                el(1, "Restaurants", "Button", (.04, .16, .28, .05)),
                el(2, "Delivery", "Button", (.36, .16, .28, .05))]
    elif stage == "results":
        rows = [el(0, y["query"], "SearchField", (.04, .08, .92, .05), editable=True, value=y["query"],
                   actions=("TAP", "TYPE"))]
        for i, place in enumerate(RAMEN):
            top = .16 + .15 * i
            rows += [el(1 + 3 * i, place.name, "Cell", (0, top, 1, .06)),
                     el(2 + 3 * i, f"{place.yelp} stars, {place.yelp_reviews:,} reviews", "StaticText",
                        (.05, top + .065, .9, .03)),
                     el(3 + 3 * i, f"Ramen, {place.distance}", "StaticText", (.05, top + .1, .6, .03))]
    elif stage in ("place", "reserve", "booked"):
        place = PLACE_BY_NAME[y["place"]]
        rows = [el(0, place.name, "NavigationBar", (0, .05, 1, .08)), el(1, "Back", "Button", (.02, .06, .12, .04)),
                el(2, f"{place.yelp} stars, {place.yelp_reviews:,} reviews", "StaticText", (.05, .15, .9, .03)),
                el(3, place.address, "StaticText", (.05, .19, .9, .03))]
        if stage == "place":
            rows.append(el(4, "Reserve", "Button", (.05, .26, .9, .06)))
        elif stage == "reserve":
            rows += [el(5 + i, day, "Button", (.05 + .3 * i, .3, .28, .05), selected=y["day"] == day)
                     for i, day in enumerate(DAYS)]
            times = place.friday_times if y["day"] == "Fri" else ("7:00 PM", "8:00 PM")
            rows += [el(10 + i, t, "Button", (.05 + .23 * (i % 4), .38 + .07 * (i // 4), .21, .05),
                        selected=y["time"] == t) for i, t in enumerate(times)]
            rows.append(el(20, "2 people", "StaticText", (.05, .55, .4, .03)))
            if y["day"] and y["time"]:
                rows.append(el(21, "Confirm reservation", "Button", (.05, .62, .9, .06)))
        else:
            day, at = y["booked"][1], y["booked"][2]
            rows.append(el(4, f"You're all set! {place.name}, {'Friday' if day == 'Fri' else day} at {at}, 2 people. "
                              "Reservation confirmed.", "StaticText", (.05, .3, .9, .06)))
    return rows


def yelp_execute(state, operation, label, text):
    y = state["yelp"]
    stage = y["stage"]
    if operation == "LAUNCH_APP":
        state["app"] = YELP
        return
    if operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT") and stage in ("home", "results"):
        y["query"] = text or ""
        if operation == "TYPE_SUBMIT" or stage == "results":
            y["stage"] = "results"
        return
    if stage == "results" and operation == "TAP" and label in PLACE_BY_NAME:
        y["stage"], y["place"], y["day"], y["time"] = "place", label, None, None
    elif stage in ("place", "reserve", "booked") and operation == "TAP" and label == "Back":
        y["stage"] = "results" if stage == "place" else "place"
    elif stage == "place" and operation == "TAP" and label == "Reserve":
        y["stage"] = "reserve"
    elif stage == "reserve" and operation == "TAP" and label in DAYS:
        y["day"], y["time"] = label, None
    elif stage == "reserve" and operation == "TAP" and re.fullmatch(r"\d{1,2}:\d{2} [AP]M", label or ""):
        y["time"] = label
    elif stage == "reserve" and operation == "TAP" and label == "Confirm reservation" and y["day"] and y["time"]:
        y["booked"] = (y["place"], y["day"], y["time"])
        y["stage"] = "booked"


def maps_execute(state, operation, label, text):
    """Maps with a ramen search: the five places. Another search goes back to the scripted Maps' search screen."""
    if operation == "LAUNCH_APP":
        state["app"] = MAPS
        return
    if operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT"):
        if "ramen" in (text or "").casefold():
            state["maps_ramen"] = text
        else:
            state["maps_ramen"], state["stage"], state["typed"] = None, "search", ""


def execute(phone, operation, target, snapshot, text=None):
    """The long-form world's side of ScriptedPhone.execute (the phone's lock is held). Logs the action with its app."""
    s = phone.state
    label = target.label if hasattr(target, "label") else (target or "")
    app = str(target) if operation == "LAUNCH_APP" else s["app"]
    if operation == "LAUNCH_APP":
        s["typed"] = ""
    if app == INSTAGRAM:
        rect = getattr(target, "rect", None)
        instagram_execute(s, operation, label, text, tab=bool(rect and rect[1] > .9))
    elif app == HINGE:
        hinge_execute(s, operation, label, text)
    elif app == YELP:
        yelp_execute(s, operation, label, text)
    elif app == MAPS:
        maps_execute(s, operation, label, text)


def record(state, operation, label, app, started):
    """One action in the timed log (``long_log``): (started, ended, op, label, app) on the monotonic clock. A tap on a
    person action, or typing in Hinge, is also a violation (``state["violations"]``): tests fail on any."""
    state["long_log"].append((started, time.monotonic(), operation, label, app))
    if ((operation in ("TAP", "LONG_PRESS", "TAP_XY") and PERSON_ACTIONS.match(label or ""))
            or (app == HINGE and operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT", "SWIPE_LEFT", "SWIPE_RIGHT"))):
        state.setdefault("violations", []).append((operation, label, app))


PERSON_ACTIONS = re.compile(r"^\s*(like|liked|like prompt|send like|super ?like|rose|send a rose|skip|pass|unmatch|"
                            r"report|block|send|message|reply|comment|follow|share|save|match)\b", re.I)


def person_taps(state):
    """Every action that acted on a person or a post: a tap on a person action (any app), a deck swipe in Hinge's
    Discover, a message typed or sent in Hinge."""
    out = []
    for started, ended, operation, label, app in state.get("long_log", ()):
        if operation in ("TAP", "LONG_PRESS", "TAP_XY") and PERSON_ACTIONS.match(label or ""):
            out.append((operation, label, app))
        elif app == HINGE and operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT"):
            out.append((operation, label, app))
    hinge = state.get("hinge") or {}
    out += [("hinge", *entry) for entry in hinge.get("liked", ()) if ("hinge", *entry) not in out]
    out += [("hinge_sent", who, text) for who, text in hinge.get("sent", ())]
    out += [("instagram", *entry) for entry in (state.get("ig") or {}).get("liked", ())]
    return out


def phone_seconds(state):
    """Seconds from the first action's start to the last action's end: how long the task held the phone."""
    log = state.get("long_log") or ()
    if not log:
        return 0.0
    return round(max(entry[1] for entry in log) - min(entry[0] for entry in log), 2)
