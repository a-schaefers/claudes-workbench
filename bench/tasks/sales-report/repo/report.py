"""Sales report: python3 report.py sales.csv"""
import argparse
import csv
import sys


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def total(rows):
    return sum(float(r["amount"]) for r in rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv")
    a = p.parse_args(argv)
    rows = load(a.csv)
    print(f"total {total(rows):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
