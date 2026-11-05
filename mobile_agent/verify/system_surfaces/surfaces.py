"""One driver per system surface. Each leaves the surface up, reads it, and returns (results, tree); ``pick`` and
``then`` are handled once, by ``play``.

The labels these drivers look for (``Edit``, ``Add Widget``, ``Done``) are the strings iOS 26 shows in English; a
simulator in another language needs the same strings in its own."""

from urllib.parse import quote, urlencode

from ..assertions import parse_selector
from .stage import SurfaceMiss

ICON_ROLES = frozenset({"Icon", "Button", "Cell"})
SEARCH_FIELD = parse_selector({"role": "field"})
# A press and hold on the Home Screen opens edit mode with an Edit button (iOS 26.4) or a menu with Edit Home Screen;
# Edit's menu and the widget's page both say Add Widget. Selectors, so an icon named like them is never tapped.
EDIT_HOME = parse_selector({"label": "/^Edit( Home Screen)?$/"})
ADD_WIDGET = parse_selector({"label": "/^\\W*Add Widget$/", "role": "button"})
# iPhone Control Center opens from the top right; Spotlight from a swipe down on the Home Screen's middle.
CONTROL_CENTER_X = 25
HOME_ROWS = (.25, .75, .03)


def empty_point(tree):
    """A point on the Home Screen that holds no icon or button: where a press and hold opens its edit menu."""
    width, height = tree.size
    covered = [node.rect for node in tree.shown() if node.role in ICON_ROLES]
    step = HOME_ROWS[2]
    row = HOME_ROWS[0]
    while row <= HOME_ROWS[1]:
        for column in (.5, .15, .85):
            x, y = width * column, height * row
            if not any(rx <= x <= rx + rw and ry <= y <= ry + rh for rx, ry, rw, rh in covered):
                return x, y
        row += step
    return None


def intent(stage, surface, ctx):
    """A shortcut that runs the app's App Intent (``shortcuts://run-shortcut``), or a phrase said to Siri."""
    params = surface.params
    if "siri" in params:
        stage.siri(params["siri"])
    else:
        given = {"input": "text", "text": params["input"]} if "input" in params else {}
        query = {"name": params["shortcut"], **given}
        # %20, not +: Shortcuts reads a + in the name as a +, and finds no shortcut (iOS 26.4 simulator, 26 Oct 2026)
        ctx.open_url("shortcuts://run-shortcut?" + urlencode(query, quote_via=quote))
    return stage.settle(surface.expect)


def widget(stage, surface, ctx):
    """The Home Screen, after adding the app's widget from the gallery when ``add`` names the app."""
    stage.home()
    if "add" in surface.params:
        _add_widget(stage, surface.params["add"])
    return stage.settle(surface.expect)


def _add_widget(stage, app):
    point = empty_point(stage.read())
    if point is None:
        raise SurfaceMiss("the Home Screen has no empty spot to press and hold; clear a row of icons")
    stage.hold(*point, "an empty spot on the Home Screen")
    stage.tap_node(stage.wait_for(None, "the Home Screen's Edit button", selector=EDIT_HOME), "Edit")
    stage.tap_node(stage.wait_for(None, "Add Widget in the Edit menu", selector=ADD_WIDGET), "Add Widget")
    stage.tap_node(stage.wait_for(None, "the widget search field", selector=SEARCH_FIELD), "the search field")
    stage.type(app)
    listed = parse_selector({"label": app, "role": "cell"})   # not the app's own icon behind the gallery
    stage.tap_node(stage.wait_for(None, f"{app} in the widget gallery", selector=listed), app)
    stage.tap_node(stage.wait_for(None, f"the Add Widget button for {app}", selector=ADD_WIDGET), "Add Widget")
    stage.tap_node(stage.wait_for("Done", "the Done button"), "Done")


def live_activity(stage, surface, ctx):
    """A Live Activity the app started, on the Lock Screen or in the Dynamic Island."""
    if surface.params.get("where", "dynamic_island") == "lock_screen":
        stage.lock()
        try:
            return stage.settle(surface.expect)
        finally:
            stage.unlock()
    stage.home()
    if surface.params.get("expanded"):
        stage.hold(stage.read().size[0] / 2, 20, "the Dynamic Island")
    return stage.settle(surface.expect)


def control_center(stage, surface, ctx):
    width, height = stage.read().size
    stage.drag((width - CONTROL_CENTER_X, 3), (width - CONTROL_CENTER_X, height * .6), "down into Control Center")
    return stage.settle(surface.expect)


def app_clip(stage, surface, ctx):
    """The App Clip, launched the way Xcode's scheme does: with its invocation URL in ``_XCAppClipURL``."""
    params = surface.params
    if "path" in params:
        ctx.install(params["path"])
    ctx.launch(params["bundle"], {"_XCAppClipURL": params["url"]})
    stage.activate(params["bundle"])
    return stage.settle(surface.expect)


def spotlight(stage, surface, ctx):
    stage.home()
    width, height = stage.read().size
    stage.drag((width / 2, height * .35), (width / 2, height * .75), "down on the Home Screen")
    stage.tap_node(stage.wait_for(None, "the Spotlight search field", selector=SEARCH_FIELD), "the search field")
    stage.type(surface.params["query"])
    return stage.settle(surface.expect)


def share_sheet(stage, surface, ctx):
    """The app's own control opens the share sheet; ``expect`` reads the sheet."""
    stage.tap_node(stage.wait_for(None, f"the control that opens the share sheet ({surface.open.render()})",
                                  selector=surface.open), "the share control")
    return stage.settle(surface.expect)


DRIVERS = {"intent": intent, "widget": widget, "live_activity": live_activity, "control_center": control_center,
           "app_clip": app_clip, "spotlight": spotlight, "share_sheet": share_sheet}
