def merge(intervals):
    """Merge overlapping or touching [start, end] intervals (any input order).

    Returns a new list of [start, end] lists sorted by start; the input is not modified.
    """
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
