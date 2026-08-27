#!/usr/bin/env python3
"""Comparison gate: crossforge python packs vs the official manylinux_2_28
images (design doc §9, M6 acceptance).

For each (version, arch), parse pyconfig.h #defines and the sysconfigdata
build_time_vars from both trees. ABI-critical keys must match exactly (exit
1 otherwise); every other difference is listed for review.

Extract the official material first, e.g.:

    cid=$(docker create --platform linux/arm64 quay.io/pypa/manylinux_2_28_aarch64)
    docker export "$cid" | tar -x -C official/aarch64 --wildcards \
        'opt/_internal/cpython-3.*/include/python3.*/pyconfig.h' \
        'opt/_internal/cpython-3.*/lib/python3.*/_sysconfigdata_*.py'
    docker rm "$cid"

Usage:
    compare-manylinux-python.py --packs /tmp/crossforge/python-packs \
        --official official/ [--versions 3.12.14,...] [--arches x86_64,aarch64]

Layout expectations: <packs>/cp<XY>-<arch>/opt/_internal/cpython-<version>/,
<official>/<arch>/opt/_internal/cpython-<version>/.
"""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_VERSIONS = ["3.9.25", "3.10.21", "3.11.16", "3.12.14", "3.13.15"]

# pyconfig.h: everything that shapes the C ABI of extension modules.
ABI_DEFINE_PREFIXES = (
    "SIZEOF_", "ALIGNOF_", "DOUBLE_", "WORDS_BIGENDIAN", "HAVE_GCC_ASM_FOR",
    "PY_BIG_ENDIAN", "VA_LIST_IS_ARRAY", "Py_DEBUG", "Py_TRACE_REFS",
    "WITH_PYMALLOC", "Py_GIL_DISABLED", "HAVE_SSIZE_T", "HAVE_LONG_LONG",
    "PY_COERCE_C_LOCALE",
)
# sysconfigdata: keys that name the wheel/extension ABI.
ABI_SYSCONFIG_KEYS = [
    "EXT_SUFFIX", "SOABI", "ABIFLAGS", "Py_DEBUG", "Py_ENABLE_SHARED",
    "SIZEOF_VOID_P", "LDVERSION", "MULTIARCH", "HOST_GNU_TYPE",
]


def defines(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        m = re.match(r"#define\s+(\w+)\s*(.*)", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def sysconfig_vars(path: Path) -> dict:
    ns = {}
    exec(path.read_text(), ns)
    return ns["build_time_vars"]


def find_one(root: Path, pattern: str) -> Path:
    hits = sorted(root.glob(pattern))
    if len(hits) != 1:
        sys.exit(f"FATAL: {root}/{pattern}: {len(hits)} matches")
    return hits[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packs", type=Path, required=True)
    ap.add_argument("--official", type=Path, required=True)
    ap.add_argument("--versions", default=",".join(DEFAULT_VERSIONS))
    ap.add_argument("--arches", default="x86_64,aarch64")
    ap.add_argument("--verbose", action="store_true",
                    help="also list non-ABI differences")
    args = ap.parse_args()

    failures = 0
    for version in args.versions.split(","):
        minor = version.rsplit(".", 1)[0]
        for arch in args.arches.split(","):
            tag = f"{version}/{arch}"
            ours_root = (args.packs / f"cp{minor.replace('.', '')}-{arch}"
                         / "opt/_internal" / f"cpython-{version}")
            official_root = (args.official / arch / "opt/_internal"
                             / f"cpython-{version}")
            if not ours_root.is_dir():
                print(f"SKIP {tag}: pack not found at {ours_root}")
                continue
            if not official_root.is_dir():
                print(f"SKIP {tag}: official tree not found at {official_root}")
                continue

            ours_h = defines(find_one(ours_root, f"include/python{minor}*/pyconfig.h"))
            official_h = defines(find_one(official_root, f"include/python{minor}*/pyconfig.h"))
            abi_bad, other = [], []
            for key in sorted(set(ours_h) | set(official_h)):
                a, b = ours_h.get(key), official_h.get(key)
                if a == b:
                    continue
                target = abi_bad if key.startswith(ABI_DEFINE_PREFIXES) else other
                target.append(f"{key}: ours={a!r} official={b!r}")

            ours_s = sysconfig_vars(find_one(ours_root, f"lib/python{minor}/_sysconfigdata_*.py"))
            official_s = sysconfig_vars(find_one(official_root, f"lib/python{minor}/_sysconfigdata_*.py"))
            s_bad = []
            for key in ABI_SYSCONFIG_KEYS:
                a, b = ours_s.get(key), official_s.get(key)
                if a != b:
                    s_bad.append(f"{key}: ours={a!r} official={b!r}")
            s_other = sum(1 for k in set(ours_s) | set(official_s)
                          if ours_s.get(k) != official_s.get(k))

            status = "PASS" if not abi_bad and not s_bad else "FAIL"
            if status == "FAIL":
                failures += 1
            print(f"{status} {tag}: pyconfig ABI diffs={len(abi_bad)}, "
                  f"pyconfig other diffs={len(other)}, "
                  f"sysconfig ABI diffs={len(s_bad)}, sysconfig other diffs={s_other}")
            for line in abi_bad:
                print(f"  ABI(pyconfig)   {line}")
            for line in s_bad:
                print(f"  ABI(sysconfig)  {line}")
            if args.verbose:
                for line in other:
                    print(f"  note(pyconfig)  {line}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
