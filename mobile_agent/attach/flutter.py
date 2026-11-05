"""Flutter's semantics tree, recognised in the accessibility tree and mapped to the agent's node model.

A Flutter app draws its own pixels and publishes a parallel semantics tree to iOS. That tree reaches WebDriverAgent as
generic ``Other`` elements: a widget's ``Semantics(label:)`` becomes the label, ``Semantics(identifier:)`` becomes the
accessibility identifier, and a tap target is a labelled ``Other`` (or ``Button``) with no further typed children. The
engine adds ``FlutterSemanticsScrollView`` elements for scrollables. The shape is recognised from those marks and from
the proportion of labelled ``Other`` nodes; it is a heuristic, proved here on recorded trees only.
"""

from dataclasses import dataclass, replace

MARKS = ("fluttersemantics", "flutterview", "flutter_view")
TYPED = frozenset({"Button", "Cell", "Link", "TextField", "SearchField", "TextView", "Switch", "Slider", "Tab",
                   "SegmentedControl", "Stepper", "Toggle", "NavigationBar", "TabBar", "Table", "CollectionView",
                   "Picker", "PickerWheel", "Key"})
MIN_NODES = 6
OTHER_SHARE = .6


@dataclass(frozen=True)
class FlutterScreen:
    """``nodes`` are the shown nodes with roles inferred; ``identifiers`` maps a Semantics identifier to its node."""
    nodes: tuple
    identifiers: dict
    marked: bool          # the engine's own element names were present (a stronger signal than shape alone)


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


def infer_role(node):
    """A labelled, enabled ``Other`` is a tappable widget: a Button."""
    if node.role != "Other" or not node.enabled or not node.label.strip():
        return node
    return replace(node, role="Button")


def read(tree):
    """The Flutter screen, or None when the tree is not Flutter's."""
    if not is_flutter(tree):
        return None
    shown = tree.shown()
    labelled = [node.path for node in shown if node.role == "Other" and node.label.strip()]
    # A labelled Other with no labelled Other below it is a widget a finger can hit.
    leaves = {path for path in labelled if not any(other.startswith(path + "/") for other in labelled)}
    nodes = tuple(infer_role(node) if node.path in leaves else node for node in shown if not _marked(node))
    identifiers = {node.identifier: node for node in nodes if node.identifier}
    return FlutterScreen(nodes, identifiers, any(_marked(node) for node in shown))
