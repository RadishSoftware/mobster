"""Pure interval accounting shared by private traces and the public offline latency report."""


def _waiting(name):
    return name == "wait" or name.startswith("wait.") or ".wait" in name


def _merged_intervals(intervals, wall_ms):
    clipped = sorted((max(0.0, start), min(wall_ms, start + duration))
                     for start, duration in intervals if duration > 0)
    merged = []
    for start, end in clipped:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def temporal_overlap_ms(phases, calls, wall_ms):
    """Off-lane call time concurrent with recorded non-wait main work, never time saved."""
    if wall_ms <= 0:
        return 0.0
    work = _merged_intervals(((item[1], item[2]) for item in phases if not _waiting(item[0])), wall_ms)
    off_lane = _merged_intervals(((item[2], item[3]) for item in calls if item[1] != "main"), wall_ms)
    total = 0.0
    i = j = 0
    while i < len(work) and j < len(off_lane):
        total += max(0.0, min(work[i][1], off_lane[j][1]) - max(work[i][0], off_lane[j][0]))
        if work[i][1] <= off_lane[j][1]:
            i += 1
        else:
            j += 1
    return total
