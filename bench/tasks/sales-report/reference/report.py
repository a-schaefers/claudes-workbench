"""Sales report: python3 report.py sales.csv"""
import argparse
import csv
import sys
from collections import defaultdict


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def total(rows):
    return sum(float(r["amount"]) for r in rows)


def totals_by(rows, field):
    sums = defaultdict(float)
    for r in rows:
        sums[r[field]] += float(r["amount"])
    return sorted(sums.items(), key=lambda kv: (-kv[1], kv[0]))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv")
    p.add_argument("--by")
    a = p.parse_args(argv)
    rows = load(a.csv)
    if a.by:
        if rows and a.by not in rows[0]:
            print(f"unknown field: {a.by}", file=sys.stderr)
            return 2
        for value, amount in totals_by(rows, a.by):
            print(f"{value} {amount:.2f}")
    print(f"total {total(rows):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
