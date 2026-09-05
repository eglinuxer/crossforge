#!/usr/bin/env python3
"""Normalize dynamic-loader output into deterministic dependency evidence."""

import re
import sys


ADDRESS_SUFFIX = re.compile(r"\s+\(0x[0-9a-fA-F]+\)\s*$")
ARROW_SPACING = re.compile(r"\s*=>\s*")
DEBUG_PREFIX = re.compile(r"^\s*[0-9]+:\s*")
DEBUG_FIND = re.compile(r"^find library=([^ ]+) \[[0-9]+\]; searching$")
DEBUG_INIT = re.compile(r"^calling init:\s*(/[^ ]+)$")


def normalize_loader_listing(text):
    dependencies = set()
    for raw_line in text.splitlines():
        line = ADDRESS_SUFFIX.sub("", raw_line).strip()
        line = ARROW_SPACING.sub(" => ", line)
        if line:
            dependencies.add(line)
    return sorted(dependencies)


def normalize_loader_debug_listing(text):
    dependencies = set()
    for raw_line in text.splitlines():
        line = DEBUG_PREFIX.sub("", raw_line).strip()
        found = DEBUG_FIND.match(line)
        if found:
            dependencies.add("needed:" + found.group(1))
            continue
        initialized = DEBUG_INIT.match(line)
        if initialized:
            dependencies.add("loaded:" + initialized.group(1))
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
