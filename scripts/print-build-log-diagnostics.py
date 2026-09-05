#!/usr/bin/env python3
import argparse
import re
import sys
from pathlib import Path


ERROR = re.compile(
    r"(?:FAILED:|fatal error:|(?<!warning:)\berror:|collect2:|"
    r"undefined reference|cannot find -l|killed(?: signal| process)?|"
    r"out of memory|no space left|internal compiler error|"
    r"ninja: build stopped)",
    re.IGNORECASE,
)


def selected_lines(lines, context, limit):
    indexes = set()
    for index, line in enumerate(lines):
        if ERROR.search(line):
            indexes.update(
                range(max(0, index - context), min(len(lines), index + context + 1))
            )
    if not indexes:
        indexes.update(range(max(0, len(lines) - limit), len(lines)))
    ordered = sorted(indexes)
    return ordered[-limit:]


def main():
    parser = argparse.ArgumentParser(
        description="Print bounded error context from a verbose build log."
    )
    parser.add_argument("log", type=Path)
    parser.add_argument("--context", type=int, default=4)
    parser.add_argument("--limit", type=int, default=240)
    parser.add_argument("--line-limit", type=int, default=2000)
    arguments = parser.parse_args()
    if arguments.context < 0 or arguments.limit < 1 or arguments.line_limit < 80:
        parser.error("diagnostic limits are outside their accepted range")

    try:
        lines = arguments.log.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError as error:
        print("error: cannot read build log: %s" % error, file=sys.stderr)
        return 1

    print("--- bounded build diagnostics: %s ---" % arguments.log, file=sys.stderr)
    previous = None
    for index in selected_lines(lines, arguments.context, arguments.limit):
        if previous is not None and index != previous + 1:
            print("...", file=sys.stderr)
        line = lines[index]
        if len(line) > arguments.line_limit:
            line = line[: arguments.line_limit] + " [line truncated]"
        print("%d: %s" % (index + 1, line), file=sys.stderr)
        previous = index
    return 0


if __name__ == "__main__":
    sys.exit(main())
