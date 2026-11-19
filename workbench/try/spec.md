Add `median` to `stats.py`.

```python
def median(xs: list[float]) -> float:
    """The middle value of xs."""
```

- The middle value of `xs` once sorted; for an even count, the mean of the two middle values.
- An empty list raises `ValueError("median of no values")`, as `mean` does.
- `xs` itself is left unchanged (sort a copy).

Add tests for it to `tests/test_stats.py` beside the ones for `mean`: an odd count, an even count,
an unsorted list, and an empty one.

Acceptance: `python3 -m unittest`
