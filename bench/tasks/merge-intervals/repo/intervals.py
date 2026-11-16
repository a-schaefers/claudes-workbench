def merge(intervals):
    """Merge overlapping or touching [start, end] intervals (any input order).

    Returns a new list of [start, end] lists sorted by start; the input is not modified.
    """
    result = []
    for interval in intervals:
        if result and interval[0] < result[-1][1]:
            result[-1][1] = interval[1]
        else:
            result.append(interval)
    return result
