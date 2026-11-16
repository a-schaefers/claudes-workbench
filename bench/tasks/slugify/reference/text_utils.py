import re


def slugify(title):
    """Lowercase words joined by single dashes, for URLs."""
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")
