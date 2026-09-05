#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 5 ]]; then
  echo "usage: $0 ARCHIVE SOURCE_ROOT BUILD_ROOT PREFIX XCB_CURSOR_PREFIX" >&2
  exit 2
fi

archive=$1
source_root=$2
build_root=$3
prefix=$4
xcb_cursor_prefix=$5
cmake_root=/opt/crossforge/host-tools/cmake/4.4.0
ninja_root=/opt/crossforge/host-tools/ninja/1.13.2
gts_root=/opt/rh/gcc-toolset-15/root/usr
modules=qtbase,qtshadertools,qtdeclarative,qttools,qtwayland,qtmultimedia,qtquick3d,qtwebengine

[[ "$archive" = /* && "$source_root" = /* && "$build_root" = /* \
  && "$prefix" = /* && "$xcb_cursor_prefix" = /* ]] || {
  echo "error: Qt configure paths must be absolute" >&2
  exit 1
}
[[ -f "$archive" ]] || {
  echo "error: authenticated Qt archive is missing" >&2
  exit 1
}
for path in "$source_root" "$build_root" "$prefix"; do
  [[ ! -e "$path" && ! -L "$path" ]] || {
    echo "error: refusing stale Qt configure path: $path" >&2
    exit 1
  }
done
[[ -x "$cmake_root/bin/cmake" && -x "$ninja_root/bin/ninja" \
  && -x "$gts_root/bin/gcc" && -x "$gts_root/bin/g++" ]] || {
  echo "error: locked Qt host toolchain is incomplete" >&2
  exit 1
}
[[ "$($cmake_root/bin/cmake --version | head -n 1)" == "cmake version 4.4.0" ]] || exit 1
[[ "$($ninja_root/bin/ninja --version)" == "1.13.2" ]] || exit 1
[[ "$($gts_root/bin/g++ -dumpfullversion)" == "15.2.1" ]] || exit 1
[[ "$(node --version)" == "v20.20.2" ]] || exit 1
PYTHONPATH=/usr/lib/python3.6/site-packages /usr/bin/python3.8 - <<'PY'
import html5lib
import six
import webencodings
assert html5lib.__version__ == "0.999999999"
PY

mkdir -p "$source_root" "$build_root"
tar --extract --xz --file "$archive" --directory "$source_root" \
  --strip-components=1 --no-same-owner --no-same-permissions
[[ -x "$source_root/configure" ]] || chmod 0755 "$source_root/configure"

export PATH="$cmake_root/bin:$ninja_root/bin:$gts_root/bin:$PATH"
export CC="$gts_root/bin/gcc"
export CXX="$gts_root/bin/g++"
export PKG_CONFIG_PATH="$xcb_cursor_prefix/lib64/pkgconfig"
export LD_LIBRARY_PATH="$xcb_cursor_prefix/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH=/usr/lib/python3.6/site-packages
export LC_ALL=C
export SOURCE_DATE_EPOCH=0

cd "$build_root"
"$source_root/configure" \
  -prefix "$prefix" \
  -release \
  -shared \
  -opensource \
  -confirm-license \
  -nomake examples \
  -nomake tests \
  -submodules "$modules" \
  -openssl-linked \
  -opengl desktop \
  -dbus \
  -system-freetype \
  -system-libjpeg \
  -system-libpng \
  -system-zlib \
  -qt-harfbuzz \
  -qt-pcre \
  -no-feature-ffmpeg \
  -- \
  -DQT_BUILD_EXAMPLES=OFF \
  -DQT_BUILD_TESTS=OFF \
  -DQT_BUILD_TOOLS_BY_DEFAULT=ON \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER="$CC" \
  -DCMAKE_CXX_COMPILER="$CXX" \
  2>&1 | tee "$build_root/configure.log"

[[ -f "$build_root/CMakeCache.txt" && -f "$build_root/config.summary" ]] || {
  echo "error: Qt host configure did not emit its cache and summary" >&2
  exit 1
}
