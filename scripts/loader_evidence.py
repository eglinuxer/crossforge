#!/usr/bin/env python3
"""Normalize dynamic-loader output into deterministic dependency evidence."""

import re
import sys


ADDRESS_SUFFIX = re.compile(r"\s+\(0x[0-9a-fA-F]+\)\s*$")
ARROW_SPACING = re.compile(r"\s*=>\s*")


def normalize_loader_listing(text):
    dependencies = set()
    for raw_line in text.splitlines():
        line = ADDRESS_SUFFIX.sub("", raw_line).strip()
        line = ARROW_SPACING.sub(" => ", line)
        if line:
            dependencies.add(line)
    return sorted(dependencies)


def main():
    dependencies = normalize_loader_listing(sys.stdin.read())
    if not dependencies:
        print("error: dynamic-loader output was empty", file=sys.stderr)
        return 1
    for dependency in dependencies:
        print(dependency)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
