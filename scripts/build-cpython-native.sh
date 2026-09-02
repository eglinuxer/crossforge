#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 6 ]]; then
  echo "usage: $0 SOURCE BUILD_DIR PREFIX VERSION ZSTD_DEPS JOBS" >&2
  exit 2
fi

source_directory=$1
build_directory=$2
prefix=$3
version=$4
zstd_directory=$5
jobs=$6
minor=${version%.*}
compact_minor=${minor/./}

[[ -x "$source_directory/configure" && -x "$source_directory/config.guess" ]] || {
  echo "error: invalid CPython source: $source_directory" >&2
  exit 1
}
[[ "$prefix" == /opt/crossforge/python/cp"$compact_minor"/build ]] || {
  echo "error: build Python prefix differs from version" >&2
  exit 1
}
[[ "$zstd_directory" == /work/deps/zstd && -d "$zstd_directory" \
    && ! -L "$zstd_directory" ]] || {
  echo "error: invalid native CPython zstd dependency context" >&2
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
  PYTHON_FOR_BUILD RANLIB READELF STRIP LIBZSTD_CFLAGS LIBZSTD_LIBS

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

zstd_enabled=0
if [[ "$minor" == 3.14 ]]; then
  zstd_enabled=1
  zstd_archive=$zstd_directory/lib/libzstd.a
  zstd_manifest=$zstd_directory/build-manifest.json
  for path in "$zstd_archive" "$zstd_manifest" \
      "$zstd_directory/include/zstd.h" "$zstd_directory/include/zdict.h"; do
    [[ -f "$path" && ! -L "$path" ]] || {
      echo "error: CPython 3.14 private zstd input is incomplete: $path" >&2
      exit 1
    }
  done
  [[ ! -e "$zstd_directory/.crossforge-empty" ]] || {
    echo "error: CPython 3.14 received the empty zstd context" >&2
    exit 1
  }
  export LIBZSTD_CFLAGS="-I$zstd_directory/include"
  export LIBZSTD_LIBS="$zstd_archive -pthread -Wl,--exclude-libs,libzstd.a"
else
  [[ -f "$zstd_directory/.crossforge-empty" ]] || {
    echo "error: pre-3.14 CPython lacks the controlled empty zstd context" >&2
    exit 1
  }
  if find "$zstd_directory" -mindepth 1 ! -name .crossforge-empty -print -quit \
      | grep -q .; then
    echo "error: pre-3.14 CPython rejects private zstd inputs" >&2
    exit 1
  fi
fi

"$source_directory/configure" \
  --build="$build" \
  --prefix="$prefix" \
  --with-pkg-config=yes \
  --with-computed-gotos=yes \
  --with-ensurepip=no \
  --disable-test-modules

if [[ "$zstd_enabled" -eq 1 ]]; then
  expected_zstd_cflags="-I$zstd_directory/include"
  expected_zstd_libs="$zstd_directory/lib/libzstd.a -pthread -Wl,--exclude-libs,libzstd.a"
  grep -Fqx "MODULE__ZSTD_STATE=yes" Makefile \
    && grep -Fqx "MODULE__ZSTD_CFLAGS=$expected_zstd_cflags" Makefile \
    && grep -Fqx "MODULE__ZSTD_LDFLAGS=$expected_zstd_libs" Makefile || {
      echo "error: CPython Makefile did not bind the private static zstd" >&2
      exit 1
    }
  if grep -E '^MODULE__ZSTD_LDFLAGS=.*(^|[[:space:]])-lzstd([[:space:]]|$)' Makefile; then
    echo "error: CPython Makefile fell back to dynamic -lzstd" >&2
    exit 1
  fi
fi

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
if sys.version_info[:3] != expected:
    raise SystemExit("native CPython version mismatch: %r != %r" % (sys.version_info[:3], expected))
' "$version"

if [[ "$zstd_enabled" -eq 1 ]]; then
  mapfile -t zstd_modules < <(
    find "$prefix/lib/python$minor/lib-dynload" -maxdepth 1 \
      -name '_zstd.*.so' -type f -print
  )
  [[ ${#zstd_modules[@]} -eq 1 ]] || {
    echo "error: CPython 3.14 must install exactly one _zstd module" >&2
    exit 1
  }
  "$python" -I -c '
import _zstd
import compression.zstd as zstd
if zstd.zstd_version_info != (1, 5, 7):
    raise SystemExit("private zstd version differs: %r" % (zstd.zstd_version_info,))
if zstd.CompressionParameter.nb_workers.bounds()[1] < 1:
    raise SystemExit("private zstd lacks multithread support")
'
  zstd_dynamic=$($READELF --wide -d "${zstd_modules[0]}")
  if grep -E '(TEXTREL|RPATH|RUNPATH|NEEDED.*libzstd)' <<<"$zstd_dynamic"; then
    echo "error: CPython _zstd has a forbidden dynamic property" >&2
    exit 1
  fi
  zstd_symbols=$($READELF --wide --dyn-syms "${zstd_modules[0]}")
  if grep -E '[[:space:]](ZSTD_|ZDICT_|FSE_|HUF_|XXH_)' <<<"$zstd_symbols"; then
    echo "error: CPython _zstd exposes private zstd symbols" >&2
    exit 1
  fi
  mkdir -p "$prefix/.crossforge"
  install -m 0644 "$zstd_manifest" "$prefix/.crossforge/zstd-build.json"
else
  [[ -z "$(find "$prefix" \( -name '_zstd.*.so' -o \
      -path '*/.crossforge/zstd-build.json' \) -print -quit)" ]] || {
    echo "error: pre-3.14 CPython unexpectedly contains private zstd output" >&2
    exit 1
  }
fi

echo "built native CPython $version at $prefix"
