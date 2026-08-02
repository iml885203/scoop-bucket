#!/usr/bin/env python3
import re
import sys

SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def parse(value):
    match = SEMVER.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid semantic version: {value}")
    prerelease = match.group(4)
    return (
        tuple(int(match.group(index)) for index in range(1, 4)),
        prerelease.split(".") if prerelease else None,
    )


def compare(left, right):
    left_core, left_pre = parse(left)
    right_core, right_pre = parse(right)
    if left_core != right_core:
        return (left_core > right_core) - (left_core < right_core)
    if left_pre is None or right_pre is None:
        return (left_pre is None) - (right_pre is None)
    for left_id, right_id in zip(left_pre, right_pre):
        if left_id == right_id:
            continue
        left_numeric = left_id.isdigit()
        right_numeric = right_id.isdigit()
        if left_numeric and right_numeric:
            return (int(left_id) > int(right_id)) - (int(left_id) < int(right_id))
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return (left_id > right_id) - (left_id < right_id)
    return (len(left_pre) > len(right_pre)) - (len(left_pre) < len(right_pre))


def self_test():
    ascending = [
        "0.7.0",
        "0.8.0-rc.1",
        "0.8.0-rc.2",
        "0.8.0",
        "1.0.0",
    ]
    for older, newer in zip(ascending, ascending[1:]):
        assert compare(older, newer) < 0
        assert compare(newer, older) > 0
    assert compare("1.0.0", "1.0.0+build.2") == 0


def main():
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        return
    if len(sys.argv) != 3:
        raise SystemExit("usage: semver-not-older.py CURRENT REQUESTED")
    current, requested = sys.argv[1:]
    if compare(requested, current) < 0:
        raise SystemExit(f"refusing to replace {current} with older release {requested}")


if __name__ == "__main__":
    main()

