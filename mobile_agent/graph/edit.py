"""Edits to a saved graph that aren't the canvas's own: the old schedule fields applied to its schedule trigger, and
what changed between two versions (a diff, and a line for the versions list)."""

from . import chain, schema


def with_schedule(graph, cron, zone):
    nodes = [dict(node) for node in graph["nodes"]]
    edges = [dict(edge) for edge in graph.get("edges") or []]
    schedule = next((node for node in nodes if node["type"] == "trigger.schedule"), None)
    if cron is None:
        if schedule is None:
            return graph
        return {**graph, "nodes": [n for n in nodes if n is not schedule],
                "edges": [e for e in edges if e["source"] != schedule["id"]]}
    if schedule is not None:
        schedule["config"] = {"cron": cron, "timezone": zone}
        return {**graph, "nodes": nodes, "edges": edges}
    taken = {node["id"] for node in nodes} | {edge["id"] for edge in edges}
    ident = chain._unique("schedule", taken)
    manual = next((node for node in nodes if node["type"] == "trigger.manual"), None)
    at = (manual or {}).get("position") or {"x": 0, "y": 0}
    nodes.append({"id": ident, "type": "trigger.schedule", "config": {"cron": cron, "timezone": zone},
                  "position": {"x": at["x"] + chain.SIDE, "y": at["y"]}})
    for edge in [e for e in edges if manual is not None and e["source"] == manual["id"]]:
        edges.append({**edge, "id": chain._unique("e" + ident, taken), "source": ident})
    return {**graph, "nodes": nodes, "edges": edges}


def diff(before, after):
    """{"nodes": {"added", "removed", "changed": [{"id", "before", "after"}]}, "edges": {"added", "removed"}}."""
    old = {node["id"]: node for node in before.get("nodes") or []}
    new = {node["id"]: node for node in after.get("nodes") or []}
    old_edges = {edge["id"]: edge for edge in before.get("edges") or []}
    new_edges = {edge["id"]: edge for edge in after.get("edges") or []}
    return {"nodes": {"added": [new[i] for i in new if i not in old], "removed": [old[i] for i in old if i not in new],
                      "changed": [{"id": i, "before": old[i], "after": new[i]} for i in new if i in old
                                  and old[i] != new[i]]},
            "edges": {"added": [new_edges[i] for i in new_edges if old_edges.get(i) != new_edges[i]],
                      "removed": [old_edges[i] for i in old_edges if new_edges.get(i) != old_edges[i]]},
            "settings": {key: {"before": before.get(key), "after": after.get(key)}
                         for key in ("inputs", "vars", "limits") if before.get(key) != after.get(key)}}


def change_line(before, after):
    """What changed between two versions' graphs, in a line: "Added 2 steps, changed Reply"."""
    if before is None:
        return "Created"
    old = {node["id"]: node for node in before.get("nodes") or []}
    new = {node["id"]: node for node in after.get("nodes") or []}

    def label(node):
        known = schema.TYPES.get(node["type"])
        return node.get("title") or (known.label if known else node["id"])

    added = [new[i] for i in new if i not in old]
    removed = [old[i] for i in old if i not in new]
    changed = [new[i] for i in new if i in old and {k: v for k, v in new[i].items() if k != "position"}
               != {k: v for k, v in old[i].items() if k != "position"}]
    moved = [i for i in new if i in old and new[i].get("position") != old[i].get("position")]
    parts = []
    if added:
        parts.append(f"Added {len(added)} step{'s' * (len(added) > 1)}" if len(added) > 1
                     else f"Added {label(added[0])}")
    if removed:
        parts.append(f"removed {len(removed)} steps" if len(removed) > 1 else f"removed {label(removed[0])}")
    if changed:
        parts.append(f"changed {len(changed)} steps" if len(changed) > 1 else f"changed {label(changed[0])}")
    if (before.get("edges") or []) != (after.get("edges") or []) and not (added or removed):
        parts.append("rewired")
    if (not parts and (moved or before.get("limits") != after.get("limits")
                       or before.get("inputs") != after.get("inputs")
                       or before.get("vars") != after.get("vars"))):
        parts.append("Rearranged" if moved else "Changed its settings")
    line = ", ".join(parts) or "Saved"
    return line[0].upper() + line[1:]
