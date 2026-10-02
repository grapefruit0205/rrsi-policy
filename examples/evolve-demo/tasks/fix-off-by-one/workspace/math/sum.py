#!/usr/bin/env python3
"""Sample data and a deliberately off-by-one sum for the fix-off-by-one task."""

SAMPLE = [3, 1, 4, 1, 5, 9, 2, 6]


def buggy_sum(xs):
    # BUG: starts at 1, skipping the first element.
    total = 0
    for i in range(1, len(xs) - 1):
        total += xs[i]
    return total


if __name__ == "__main__":
    print(buggy_sum(SAMPLE))
