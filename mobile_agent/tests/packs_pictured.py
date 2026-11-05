"""Synthetic screens as iPhone Mirroring reads them, with every name and number made up."""

from mobile_agent import perception
from mobile_agent.state import Element

TABS_IG = [("Home", .11), ("Reels", .31), ("Messages", .51), ("Search", .70), ("Profile", .90)]


def ig_tabs(selected=None):
    return [(label, x - .05, .91, .1, .04, label == selected) for label, x in TABS_IG]



# (app, name) -> (the screen it is, lines [(text, x, y, w, h)], icons [(label, x, y, w, h, selected)])
SCREENS = {
    ("instagram", "feed"): ("feed", [
        ("9:41", .1, .03, .1, .02), ("Instagram", .4, .09, .2, .03), ("Your story", .05, .25, .16, .02),
        ("story.one", .32, .25, .16, .02), ("bakery.daily •", .13, .29, .25, .02),
        ("2,004", .1, .71, .1, .02), ("Liked by fan.one and others", .03, .75, .6, .02),
        ("bakery.daily Sourdough fresh out of the oven... more", .03, .78, .9, .02),
        ("9 hours ago", .03, .8, .2, .02)],
        [("Create", .03, .08, .08, .04, False), ("Notifications", .89, .08, .08, .04, False),
         ("Like", .03, .7, .06, .03, False), ("Save", .9, .7, .06, .03, False), *ig_tabs()]),
    ("instagram", "feed-scrolled"): ("feed", [
        ("Instagram", .4, .09, .2, .03), ("trail.runner • Original audio", .13, .2, .5, .02),
        ("0:15", .9, .25, .06, .02), ("Hill repeats, week 3", .1, .8, .6, .02)],
        [("Create", .03, .08, .08, .04, False), ("Notifications", .89, .08, .08, .04, False),
         ("More", .9, .2, .06, .03, False), *ig_tabs()]),
    ("instagram", "reels"): ("reels", [
        ("Reels Friends", .35, .09, .3, .03), ("282K", .89, .46, .07, .02), ("2,693", .89, .55, .07, .02),
        ("25.6K", .89, .64, .07, .02), ("142K", .89, .72, .07, .02), ("dance.studio •", .12, .78, .25, .02),
        ("Follow", .42, .78, .1, .02), ("Three moves for beginners #dance", .04, .82, .7, .02),
        ("Liked by friend.two and 282,188 others", .1, .86, .7, .02)],
        [("Like", .89, .41, .07, .03, False), ("Comment", .89, .5, .07, .03, False), ("Save", .89, .76, .07, .03,
                                                                                      False), *ig_tabs("Reels")]),
    ("instagram", "reel-opened"): ("reels", [
        ("446K", .89, .46, .07, .02), ("400", .89, .55, .07, .02), ("7,553", .89, .64, .07, .02),
        ("231K", .89, .72, .07, .02), ("code.tips o", .15, .81, .2, .02), ("Follow", .42, .81, .1, .02),
        ("Three Python one-liners", .2, .86, .5, .02), ("Add comment...", .1, .93, .3, .02)],
        [("Back", .04, .08, .06, .03, False), ("Search", .89, .08, .06, .03, False),
         ("Send", .89, .67, .07, .03, False), ("More", .89, .84, .07, .03, False)]),
    ("instagram", "explore"): ("explore", [
        ("Search with Meta Al", .12, .09, .5, .03), ("For you", .19, .15, .12, .02), ("Cooking", .39, .15, .12, .02),
        ("© 788K", .72, .37, .1, .02), ("© 1.1M", .04, .58, .1, .02)],
        [("Save", .9, .08, .06, .03, False), *ig_tabs()]),
    ("instagram", "search"): ("search", [
        ("space", .15, .09, .3, .03), ("space fan page", .18, .15, .5, .02), ("space.fan.page • 2.7M", .18, .17, .5,
                                                                               .02),
        ("space agency", .18, .22, .4, .02), ("space • 104M followers", .18, .24, .45, .02)],
        [("Back", .03, .08, .06, .03, False)]),
    ("instagram", "comments"): ("comments", [
        ("Comments", .39, .33, .22, .02), ("fan.one 6d", .13, .39, .2, .02), ("Love the colours in this one", .13,
                                                                              .41, .7, .02),
        ("Reply", .13, .46, .1, .02), ("View 1 more reply", .21, .49, .28, .02), ("Add a comment...", .18, .91, .5,
                                                                                  .03)],
        [("Like", .91, .41, .04, .03, False)]),
    ("instagram", "dms"): ("dm_inbox", [
        ("me.test", .3, .07, .4, .03), ("Ask Meta AI or Search", .1, .12, .8, .03), ("Messages", .04, .2, .3, .02),
        ("Requests", .75, .2, .2, .02), ("Sam Lee", .2, .3, .4, .02), ("4 new messages · 3h", .2, .32, .5, .02),
        ("Riley", .2, .38, .3, .02), ("haha see you then · 5h", .2, .4, .5, .02)],
        [("New message", .88, .07, .07, .03, False), ("Camera", .88, .3, .06, .03, False), *ig_tabs("Messages")]),
    ("instagram", "thread"): ("dm_thread", [
        ("Sam Lee", .3, .07, .4, .02), ("Active now", .3, .09, .3, .02), ("are we still on for friday?", .04, .24,
                                                                         .6, .03),
        ("yes! what time works?", .4, .3, .5, .03), ("Message...", .12, .9, .5, .03)],
        [("Back", .02, .07, .06, .03, False), ("Voice message", .75, .9, .06, .03, False),
         ("Gallery", .84, .9, .06, .03, False)]),
    ("instagram", "profile"): ("profile", [
        ("space", .3, .07, .3, .03), ("4,021", .34, .13, .1, .02), ("posts", .34, .15, .1, .02),
        ("104M", .54, .13, .1, .02), ("followers", .54, .15, .14, .02), ("77", .78, .13, .06, .02),
        ("following", .76, .15, .14, .02), ("Follow", .1, .3, .3, .03), ("Message", .55, .3, .3, .03)],
        [("Back", .02, .07, .06, .03, False)]),
    ("instagram", "saved"): ("saved", [
        ("Saved", .4, .07, .2, .03), ("All posts", .1, .37, .3, .02), ("Recipes", .55, .37, .3, .02)],
        [("Back", .02, .07, .06, .03, False)]),
    ("tiktok", "foryou"): ("for_you", [
        ("LIVE", .05, .06, .1, .02), ("Following", .3, .06, .2, .02), ("For You", .53, .06, .2, .02),
        ("120.4K", .87, .55, .1, .02), ("1,203", .87, .64, .1, .02), ("8,812", .87, .73, .1, .02),
        ("2,450", .87, .82, .1, .02), ("street.chef", .04, .76, .3, .02),
        ("Smash burger in 90 seconds #food", .04, .8, .7, .02), ("♫ original sound - street.chef", .04, .86, .6, .02),
        ("Home", .06, .95, .1, .02), ("Friends", .26, .95, .12, .02), ("Inbox", .66, .95, .1, .02),
        ("Profile", .86, .95, .12, .02)], []),
    ("tiktok", "comments"): ("comments", [
        ("1,203 comments", .35, .3, .3, .02), ("grill.fan", .14, .36, .2, .02), ("That crust though", .14, .38, .5,
                                                                               .02),
        ("Reply", .14, .41, .1, .02), ("Add comment...", .05, .92, .5, .03)], [("Close", .88, .3, .06, .03, False)]),
    ("tiktok", "inbox"): ("inbox", [
        ("Inbox", .4, .06, .2, .03), ("Activity", .2, .13, .3, .02), ("New followers", .2, .19, .3, .02),
        ("Mia Chen", .2, .27, .3, .02), ("sent a video · 2h", .2, .29, .4, .02), ("Home", .06, .95, .1, .02),
        ("Inbox", .66, .95, .1, .02), ("Profile", .86, .95, .12, .02)], []),
    ("tiktok", "profile"): ("profile", [
        ("Space Facts", .3, .06, .4, .03), ("@space.facts", .35, .24, .3, .02), ("12", .2, .28, .06, .02),
        ("Following", .15, .31, .15, .02), ("2.1M", .45, .28, .1, .02), ("Followers", .42, .31, .16, .02),
        ("48.5M", .72, .28, .1, .02), ("Likes", .72, .31, .1, .02), ("Follow", .25, .37, .3, .03),
        ("Message", .62, .37, .2, .03)], [("Back", .02, .06, .06, .03, False)]),
    ("x", "home"): ("home", [
        ("For you", .15, .1, .2, .02), ("Following", .6, .1, .2, .02),
        ("Space Agency @space.agency · 2h", .15, .2, .7, .02), ("The Moon is bright tonight.", .15, .23, .7, .02),
        ("12 replies 40 reposts 1,203 likes", .15, .27, .6, .02),
        ("Cook Daily @cook.daily · 5h", .15, .35, .7, .02), ("Ten minute ramen, recipe below", .15, .38, .7, .02)],
        [("Home", .06, .93, .08, .03, False), ("Search", .26, .93, .08, .03, False),
         ("Notifications", .66, .93, .08, .03, False), ("Messages", .86, .93, .08, .03, False)]),
    ("x", "profile"): ("profile", [
        ("Space Agency", .05, .3, .4, .03), ("@space.agency", .05, .33, .3, .02), ("12 Following", .05, .4, .2, .02),
        ("2.1M Followers", .3, .4, .25, .02), ("Posts", .05, .47, .1, .02), ("Replies", .25, .47, .12, .02),
        ("Follow", .75, .25, .2, .03)], [("Back", .03, .06, .06, .03, False)]),
    ("youtube", "home"): ("home", [
        ("All", .05, .12, .06, .02), ("Music", .14, .12, .1, .02), ("Gaming", .27, .12, .12, .02),
        ("How big is the Sun, really", .15, .45, .7, .02), ("Space Facts · 2.1M views · 3 days ago", .15, .48, .7, .02),
        ("Home", .06, .95, .1, .02), ("Shorts", .25, .95, .1, .02), ("Subscriptions", .62, .95, .2, .02),
        ("You", .88, .95, .06, .02)], []),
    ("youtube", "shorts"): ("shorts", [
        ("12K", .88, .5, .08, .02), ("410", .88, .6, .08, .02), ("Share", .88, .7, .08, .02),
        ("@space.facts", .04, .8, .3, .02), ("Subscribe", .38, .8, .2, .03), ("Why the Sun looks yellow", .04, .84, .6,
                                                                                 .02),
        ("Shorts", .25, .95, .1, .02)], []),
    ("threads", "home"): ("home", [
        ("For you", .4, .08, .2, .02), ("What's new?", .2, .14, .4, .02), ("plant.mom 3h", .15, .22, .3, .02),
        ("Repotting a monstera today", .15, .25, .6, .02), ("city.walks 6h", .15, .35, .3, .02),
        ("Tokyo at 5am", .15, .38, .4, .02)], [("Home", .06, .93, .08, .03, False)]),
    ("linkedin", "home"): ("home", [
        ("Search", .2, .07, .5, .03), ("Start a post", .2, .14, .4, .02), ("Dana Park", .15, .22, .3, .02),
        ("Product designer at Radish Software", .15, .24, .4, .02), ("Excited to share our new app!", .05, .3, .7, .02),
        ("120 reactions · 14 comments", .05, .45, .5, .02), ("Home", .05, .95, .1, .02),
        ("My Network", .22, .95, .15, .02), ("Notifications", .62, .95, .18, .02), ("Jobs", .85, .95, .08, .02)], []),
    ("linkedin", "messages"): ("messages", [
        ("Messaging", .3, .07, .4, .03), ("Search messages", .1, .13, .6, .03), ("Focused", .05, .2, .15, .02),
        ("Sam Rivera", .2, .27, .3, .02), ("Thanks for the intro · 2d", .2, .29, .5, .02)],
        [("Back", .02, .07, .06, .03, False)]),
    ("reddit", "home"): ("home", [
        ("Home", .4, .07, .2, .02), ("r/running · 5h", .15, .15, .4, .02),
        ("What shoes for a first marathon?", .05, .19, .8, .02), ("1.2K", .1, .3, .08, .02), ("340", .4, .3, .08,
                                                                                            .02),
        ("r/cooking · 9h", .15, .38, .4, .02), ("My ten minute ramen", .05, .42, .6, .02),
        ("Home", .05, .95, .1, .02), ("Communities", .2, .95, .18, .02), ("Chat", .65, .95, .08, .02),
        ("Inbox", .85, .95, .1, .02)], []),
}


def screen(app, name, bundle):
    """The Snapshot the Mirroring driver hands the loop for this synthetic reading."""
    _, lines, icons = SCREENS[(app, name)]
    reading = {"rows": [{"text": t, "confidence": .9, "rect": [x, y, w, h]} for t, x, y, w, h in lines],
               "hash": f"{app}-{name}", "window": {"w": 318, "h": 701}}
    snap = perception.snapshot_from(reading, source="mirroring", bundle=bundle)
    named = [Element(f"i{i}", label, "Button", (x, y, w, h), locator=f"icon:{i}", selected=bool(selected))
             for i, (label, x, y, w, h, selected) in enumerate(icons)]
    snap.elements = [*snap.elements, *named]
    snap.text = "\n".join(e.label for e in snap.elements)
    return snap
