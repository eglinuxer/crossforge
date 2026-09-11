#!/usr/bin/env python3
"""Run every configuration regression in bounded, isolated Python workers."""

from crossforge_internal.ci_tests import main


if __name__ == "__main__":
    raise SystemExit(main())
