"""Recognise Flutter from engine marks or the shape of its accessibility tree.

The shape heuristic uses labelled Other nodes and typed-control density. It is
covered by synthetic fixtures, not calibrated against a real-app survey.
"""

MARKS = ("fluttersemantics", "flutterview", "flutter_view")
TYPED = frozenset({"Button", "Cell", "Link", "TextField", "SearchField", "TextView", "Switch", "Slider", "Tab",
                   "SegmentedControl", "Stepper", "Toggle", "NavigationBar", "TabBar", "Table", "CollectionView",
                   "Picker", "PickerWheel", "Key"})
MIN_NODES = 6
OTHER_SHARE = .6


def _marked(node):
    return any(mark in f"{node.identifier} {node.label}".casefold().replace(" ", "") for mark in MARKS)


def is_flutter(tree):
    """Whether the shown tree has Flutter's shape: engine marks, or mostly labelled ``Other`` and few typed controls."""
    shown = tree.shown()
    if any(_marked(node) for node in shown):
        return True
    labelled = [node for node in shown if node.label.strip() or node.identifier]
    if len(labelled) < MIN_NODES:
        return False
    other = sum(node.role == "Other" for node in labelled)
    return other / len(labelled) >= OTHER_SHARE and sum(node.role in TYPED for node in shown) <= len(labelled) // 4
