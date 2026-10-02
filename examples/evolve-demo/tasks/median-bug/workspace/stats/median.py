#!/usr/bin/env python3
"""Deliberately buggy median: wrong for even-length lists."""

def median(xs):
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    # BUG: returns the larger middle element instead of the average.
    return s[mid]


if __name__ == "__main__":
    print(median([1, 2, 3, 4]))
