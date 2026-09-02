#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 6 ]]; then
  echo "usage: $0 SOURCE BUILD_DIR PREFIX VERSION ADAPTER JOBS" >&2
  exit 2
fi

source_directory=$1
build_directory=$2
prefix=$3
version=$4
adapter=$5
jobs=$6
minor=${version%.*}
compact_minor=${minor/./}

[[ -x "$source_directory/configure" && -x "$source_directory/config.guess" ]] || {
  echo "error: invalid CPython source: $source_directory" >&2
  exit 1
}
[[ "$version" =~ ^3\.[0-9]+\.[0-9]+$ && "$adapter" == modern ]] || {
  echo "error: unsupported CPython version/adapter: $version/$adapter" >&2
  exit 1
}
[[ "$prefix" == /opt/crossforge/python/cp"$compact_minor"/build ]] || {
  echo "error: build Python prefix differs from version" >&2
  exit 1
}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || {
  echo "error: JOBS must be positive" >&2
  exit 1
}

build=$($source_directory/config.guess)
[[ "$build" == x86_64-*-linux-gnu ]] || {
  echo "error: build Python must run on linux/x86_64" >&2
  exit 1
}

mkdir -p "$build_directory"
cd "$build_directory"

export CFLAGS='-O2 -g -pipe -ffile-prefix-map=/work=/usr/src/debug/crossforge'
export CXXFLAGS=$CFLAGS
export LDFLAGS='-Wl,-z,relro,-z,now'
export SOURCE_DATE_EPOCH=0
unset HOSTRUNNER PYTHON_FOR_BUILD

"$source_directory/configure" \
  --build="$build" \
  --prefix="$prefix" \
  --with-pkg-config=yes \
  --with-computed-gotos=yes \
  --with-ensurepip=no \
  --disable-test-modules

make -j"$jobs"
make install

python=$prefix/bin/python$minor
[[ -x "$python" ]] || {
  echo "error: build Python was not installed" >&2
  exit 1
}
"$python" -I -c '
import _bz2, _ctypes, _hashlib, _lzma, _sqlite3, _ssl, _uuid, zlib
import sys
expected = tuple(map(int, sys.argv[1].split(".")))
assert sys.version_info[:3] == expected, (sys.version_info, expected)
' "$version"

echo "built native CPython $version at $prefix"
