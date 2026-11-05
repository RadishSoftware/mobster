"""File a finished result as an issue through a connected tracker (GitHub, Linear or Jira): one tool call of the
connector the person already connected, through the same ConnectorService.call a workflow's connector step uses."""

import re

# preset -> [(tool, the arguments for it)], the first tool the connector offers wins. ``target`` is what only the
# person knows (GitHub's owner and repo, Linear's team, Jira's cloudId and projectKey); the title and text are ours.
TRACKERS = {
    "github": (("issue_write", lambda t, title, text: {"method": "create", **t, "title": title, "body": text}),
               ("create_issue", lambda t, title, text: {**t, "title": title, "body": text})),
    "linear": (("save_issue", lambda t, title, text: {**t, "title": title, "description": text}),
               ("create_issue", lambda t, title, text: {**t, "title": title, "description": text})),
    "jira": (("createJiraIssue", lambda t, title, text: {"issueTypeName": "Bug", **t, "summary": title,
                                                         "description": text}),),
}
TARGET_KEYS = {"owner", "repo", "team", "teamId", "project", "projectId", "cloudId", "projectKey", "issueTypeName",
               "labels"}
URL = re.compile(r"https://[^\s\"'<>)]+")


def trackers(rows):
    """The enabled connectors that are one of the trackers, in the order they were added."""
    return [row for row in rows if row.get("enabled") and row.get("preset") in TRACKERS]


def pick(rows, wanted):
    """The tracker connector ``wanted`` names (its id, name or preset), else the only one connected.
    Returns (row, None) or (None, (code, message)) when no suitable one is connected."""
    found = trackers(rows)
    if not found:
        return None, ("no_tracker", "No issue tracker is connected. Connect GitHub, Linear or Jira in Settings › "
                                    "Connectors, then file the issue.")
    if wanted:
        wanted = str(wanted).lower()
        found = [row for row in found if wanted in (row["id"].lower(), row["name"].lower(), row["preset"])]
        if not found:
            return None, ("no_tracker", f"{wanted} isn't a connected issue tracker. Pick one that is connected.")
    if len(found) > 1:
        return None, ("tracker_ambiguous", "Several issue trackers are connected: " +
                      ", ".join(row["name"] for row in found) + ". Name the one to use.")
    return found[0], None


def tool_for(row, target, title, text):
    """(tool, arguments) for the first create-issue tool the connector offers, else None."""
    offered = {tool["name"] for tool in row.get("tools") or ()}
    for tool, build in TRACKERS[row["preset"]]:
        if tool in offered:
            return tool, build(target, title, text)
    return None


def clean_target(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - TARGET_KEYS:
        raise ValueError("target holds only " + ", ".join(sorted(TARGET_KEYS)))
    if not all(isinstance(item, (str, list)) for item in value.values()):
        raise ValueError("Every target value is text (labels is a list)")
    return dict(value)


def link_of(text):
    """The first https link in the tracker's answer (the issue's address), or None."""
    found = URL.search(text or "")
    return found.group(0).rstrip(".,") if found else None
