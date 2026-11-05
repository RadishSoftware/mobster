"""WebView detection for the accessibility-based app score."""


def web_view(tree):
    """The largest shown WebView in the accessibility tree, or None."""
    views = [node for node in tree.shown() if node.role == "WebView"]
    return max(views, key=lambda node: node.rect[2] * node.rect[3], default=None)


def inside(node, view):
    x, y, w, h = node.rect
    vx, vy, vw, vh = view.rect
    return node is view or (x >= vx and y >= vy and x + w <= vx + vw + 1 and y + h <= vy + vh + 1)
