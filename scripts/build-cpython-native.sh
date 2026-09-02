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
script_directory=$(cd "$(dirname "$0")" && pwd)
contract_checker=$script_directory/python_row_contract.py
platform_python=/usr/libexec/platform-python

[[ -x "$source_directory/configure" && -x "$source_directory/config.guess" ]] || {
  echo "error: invalid CPython source: $source_directory" >&2
  exit 1
}
[[ -x "$platform_python" && -f "$contract_checker" ]] || {
  echo "error: CPython row contract checker is missing" >&2
  exit 1
}
"$platform_python" "$contract_checker" check "$version" "$adapter"
[[ "$prefix" == /opt/crossforge/python/cp"$compact_minor"/build ]] || {
  echo "error: build Python prefix differs from version" >&2
  exit 1
}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || {
  echo "error: JOBS must be positive" >&2
  exit 1
}
[[ ! -e "$build_directory" && ! -L "$build_directory" ]] || {
  echo "error: refusing stale native CPython build directory: $build_directory" >&2
  exit 1
}
[[ ! -e "$prefix" && ! -L "$prefix" ]] || {
  echo "error: refusing stale build Python prefix: $prefix" >&2
  exit 1
}
while IFS='=' read -r name unused; do
  case "$name" in
    PYTHON*|_PYTHON*|ac_cv_*) unset "$name" ;;
  esac
done < <(env)
unset AR AS BLDSHARED CCSHARED CFLAGS CONFIG_SITE CPATH CPP CPPFLAGS CC \
  CXX CXXFLAGS C_INCLUDE_PATH CPLUS_INCLUDE_PATH HOSTRUNNER LD LDFLAGS \
  LDLIBS LD_LIBRARY_PATH LD_PRELOAD LIBRARY_PATH LIBS LINKFORSHARED \
  LDSHARED MAKEFLAGS MFLAGS NM OBJC_INCLUDE_PATH OBJDUMP OBJCOPY \
  PKG_CONFIG PKG_CONFIG_LIBDIR PKG_CONFIG_PATH PKG_CONFIG_SYSROOT_DIR \
  PYTHON_FOR_BUILD RANLIB READELF STRIP

build=$($source_directory/config.guess)
[[ "$build" == x86_64-*-linux-gnu ]] || {
  echo "error: build Python must run on linux/x86_64" >&2
  exit 1
}
host_toolchain=/opt/rh/gcc-toolset-15/root/usr/bin
for tool in gcc g++ ar ranlib readelf ld nm strip objcopy; do
  [[ -x "$host_toolchain/$tool" ]] || {
    echo "error: missing locked host tool: $tool" >&2
    exit 1
  }
done

mkdir -p "$build_directory"
cd "$build_directory"

export PATH="$host_toolchain:/usr/bin:/bin"
export CC=$host_toolchain/gcc
export CXX=$host_toolchain/g++
export CPP="$host_toolchain/gcc -E"
export AR=$host_toolchain/ar
export RANLIB=$host_toolchain/ranlib
export READELF=$host_toolchain/readelf
export LD=$host_toolchain/ld
export NM=$host_toolchain/nm
export STRIP=$host_toolchain/strip
export OBJCOPY=$host_toolchain/objcopy
export CFLAGS='-O2 -g -pipe -ffile-prefix-map=/work=/usr/src/debug/crossforge'
export CXXFLAGS=$CFLAGS
export LDFLAGS='-Wl,-z,relro,-z,now'
export PKG_CONFIG=/usr/bin/pkg-config
export PKG_CONFIG_PATH=
export SOURCE_DATE_EPOCH=0

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
