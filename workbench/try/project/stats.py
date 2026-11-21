"""A tiny statistics module."""
from __future__ import annotations


def mean(xs: list[float]) -> float:
    """The arithmetic mean of xs."""
    if not xs:
        raise ValueError("mean of no values")
    return sum(xs) / len(xs)
