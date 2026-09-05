#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 10 ]]; then
  echo "usage: $0 ARCHIVE SOURCE_ROOT BUILD_ROOT INSTALL_ROOT ARCH TRIPLE SYSROOT QT_HOST_PATH PATCH PATCH_SHA256" >&2
  exit 2
fi

archive=$1
source_root=$2
build_root=$3
install_root=$4
arch=$5
triple=$6
sysroot=$7
qt_host_path=$8
patch_file=$9
patch_sha256=${10}
cmake_root=/opt/crossforge/host-tools/cmake/4.4.0
ninja_root=/opt/crossforge/host-tools/ninja/1.13.2
toolchain_root=/opt/crossforge/targets/$triple
toolchain_file=/work/integration/cmake/$triple.cmake
modules=qtbase,qtshadertools,qtdeclarative,qttools,qtwayland,qtmultimedia,qtquick3d,qtwebengine

case "$arch:$triple:$sysroot:$install_root" in
  x86_64:x86_64-unknown-linux-gnu:/opt/crossforge/sysroots/el8/x86_64:/opt/crossforge/qualification/qt/6.8.4/targets/x86_64-unknown-linux-gnu|\
  aarch64:aarch64-unknown-linux-gnu:/opt/crossforge/sysroots/el8/aarch64:/opt/crossforge/qualification/qt/6.8.4/targets/aarch64-unknown-linux-gnu) ;;
  *) echo "error: Qt target configure identity differs" >&2; exit 1 ;;
esac
[[ "$archive" = /* && "$source_root" = /* && "$build_root" = /* \
  && "$patch_file" = /* ]] || {
  echo "error: Qt target configure paths must be absolute" >&2
  exit 1
}
for path in "$source_root" "$build_root" "$install_root"; do
  [[ ! -e "$path" && ! -L "$path" ]] || {
    echo "error: refusing stale Qt target configure path: $path" >&2
    exit 1
  }
done
[[ -f "$archive" && -f "$patch_file" && -f "$toolchain_file" \
  && -x "$cmake_root/bin/cmake" && -x "$ninja_root/bin/ninja" \
  && -x "$toolchain_root/bin/$triple-gcc" \
  && -x "$toolchain_root/bin/$triple-g++" \
  && -x "$qt_host_path/bin/qtpaths6" \
  && -f "$qt_host_path/qt-host-build.json" \
  && -f "$sysroot/usr/lib64/libxcb-cursor.so.0.0.0" \
  && -f "$sysroot/usr/lib64/libavcodec.so.61.19.101" ]] || {
  echo "error: Qt target configure input closure is incomplete" >&2
  exit 1
}
[[ "$patch_sha256" =~ ^[0-9a-f]{64}$ ]] || {
  echo "error: Qt target patch digest is invalid" >&2
  exit 1
}
printf '%s  %s\n' "$patch_sha256" "$patch_file" \
  | sha256sum --check --status - || {
  echo "error: Qt target patch digest differs" >&2
  exit 1
}
[[ "$($toolchain_root/bin/$triple-gcc -dumpmachine)" == "$triple" \
  && "$($cmake_root/bin/cmake --version | head -n 1)" == "cmake version 4.4.0" \
  && "$($ninja_root/bin/ninja --version)" == "1.13.2" ]] || {
  echo "error: Qt target configure tool identity differs" >&2
  exit 1
}
[[ -z "${HOSTRUNNER:-}" && -z "${CMAKE_CROSSCOMPILING_EMULATOR:-}" ]] || {
  echo "error: Qt target configure forbids execution adapters" >&2
  exit 1
}

mkdir -p "$source_root" "$build_root"
tar --extract --xz --file "$archive" --directory "$source_root" \
  --strip-components=1 --no-same-owner --no-same-permissions
[[ -x "$source_root/configure" ]] || chmod 0755 "$source_root/configure"
if ! patch --batch --forward --fuzz=0 --strip=1 \
  --directory="$source_root" --input="$patch_file" \
  >"$build_root/patch.log" 2>&1; then
  cat "$build_root/patch.log" >&2
  exit 1
fi
for relative in \
  qtwebengine/src/3rdparty/chromium/third_party/xnnpack/src/src/amalgam/gen/neonfp16arith.c \
  qtwebengine/src/3rdparty/chromium/third_party/xnnpack/src/src/qs8-f16-vcvt/neon.c.in; do
  grep -F 'vreinterpretq_f16_u16(vld1q_dup_u16(&params->neon.scale))' \
    "$source_root/$relative" >/dev/null || {
    echo "error: Qt target XNNPACK patch postcondition differs" >&2
    exit 1
  }
  if grep -F 'vld1q_dup_f16(&params->neon.scale)' \
    "$source_root/$relative" >/dev/null; then
    echo "error: Qt target XNNPACK incompatible load remains" >&2
    exit 1
  fi
done

export PATH="$cmake_root/bin:$ninja_root/bin:$toolchain_root/bin:$PATH"
export CC="$toolchain_root/bin/$triple-gcc"
export CXX="$toolchain_root/bin/$triple-g++"
export PKG_CONFIG_SYSROOT_DIR="$sysroot"
export PKG_CONFIG_LIBDIR="$sysroot/usr/lib64/pkgconfig:$sysroot/usr/share/pkgconfig"
export PYTHONPATH=/usr/lib/python3.6/site-packages
export LC_ALL=C.UTF-8
export SOURCE_DATE_EPOCH=0

cd "$build_root"
"$source_root/configure" \
  -prefix /usr \
  -extprefix "$install_root/usr" \
  -qt-host-path "$qt_host_path" \
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
  -- \
  -DCMAKE_TOOLCHAIN_FILE="$toolchain_file" \
  -DFFMPEG_DIR="$sysroot/usr" \
  -DPKG_CONFIG_HOST_EXECUTABLE=/usr/bin/pkg-config \
  -DQT_BUILD_EXAMPLES=OFF \
  -DQT_BUILD_TESTS=OFF \
  -DQT_BUILD_TOOLS_BY_DEFAULT=OFF \
  -DCMAKE_BUILD_TYPE=Release \
  2>&1 | tee "$build_root/configure.log"

[[ -f "$build_root/CMakeCache.txt" && -f "$build_root/config.summary" ]] || {
  echo "error: Qt target configure did not emit its cache and summary" >&2
  exit 1
}
