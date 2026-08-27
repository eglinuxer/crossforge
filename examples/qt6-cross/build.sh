#!/usr/bin/env bash
# Cross-build Qt 6 and run a Qt program on the target.
#
# Three stages, resumable: each records a marker, so re-running after a
# failure picks up where it stopped instead of rebuilding Qt from scratch.
set -euo pipefail

QT_VERSION="${QT_VERSION:-6.8.4}"
QT_SHA256="${QT_SHA256:-532dfbf3fa3cbc68fa37441ea9e81c5009da044eaecda78ffaeafd8bd125532f}"
WORK="${WORK:-/tmp/crossforge}"
IMAGE="${IMAGE:-crossforge-crossenv:el8-aarch64}"
# No HOST_ID knob to match: stage 1 calls plain gcc/g++, which the image
# puts on PATH pointing at the native companion. Only the cross side needs
# naming, because its toolchain file and sysroot are addressed by path.
TARGET_ID="${TARGET_ID:-gcc14.2.1-el8-qt6-aarch64}"
TRIPLE="${TRIPLE:-aarch64-unknown-linux-gnu}"
JOBS="${JOBS:-$(nproc)}"

QT_ROOT="$WORK/qt6"
SRC="$QT_ROOT/qtbase-everywhere-src-$QT_VERSION"
HOST_QT="$QT_ROOT/host"
TARGET_QT="$QT_ROOT/target"
SAMPLE_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n=== %s\n' "$*"; }

# Everything builds inside the environment image, which is where both
# toolchains and the host build tools live. Paths are bound at their own
# location so anything recorded in a build tree stays valid outside it.
in_env() {
  docker run --rm \
    -v "$WORK:$WORK" -v "$SAMPLE_SRC:$SAMPLE_SRC" \
    --user "$(id -u):$(id -g)" \
    -w "${CWD:-$WORK}" "$IMAGE" bash -c "$1"
}

mkdir -p "$QT_ROOT"

say "source"
if [ ! -d "$SRC" ]; then
  TARBALL="$QT_ROOT/qtbase-$QT_VERSION.tar.xz"
  MAJOR_MINOR="${QT_VERSION%.*}"
  [ -f "$TARBALL" ] || curl -fL --retry 3 -o "$TARBALL" \
    "https://download.qt.io/official_releases/qt/$MAJOR_MINOR/$QT_VERSION/submodules/qtbase-everywhere-opensource-src-$QT_VERSION.tar.xz"
  echo "$QT_SHA256  $TARBALL" | sha256sum -c -
  tar -xf "$TARBALL" -C "$QT_ROOT"
fi
echo "  $SRC"

# Stage 1. The host build exists to produce moc, rcc, qt-cmake and syncqt,
# so it is deliberately Core-only: a GUI host build would need X11 and
# fontconfig development packages on the build side for tools nothing here
# runs.
say "stage 1: host Qt (native x86_64)"
# Qt 6 installs moc, rcc and syncqt into libexec/, not bin/ — bin/ holds
# qt-cmake and qmake. Worth knowing before wondering where the tools went.
if [ ! -x "$HOST_QT/libexec/moc" ]; then
  mkdir -p "$QT_ROOT/build-host"
  CWD="$QT_ROOT/build-host" in_env "
    set -e
    [ -f CMakeCache.txt ] || '$SRC/configure' -prefix '$HOST_QT' -release \
      -no-gui -no-widgets -nomake examples -nomake tests -no-openssl -no-icu \
      -- -DCMAKE_BUILD_TYPE=Release
    cmake --build . --parallel $JOBS
    cmake --install ."
else
  echo "  already built"
fi
"$HOST_QT/libexec/moc" --version

# Stage 2. The target build runs those tools while compiling for aarch64.
# QT_HOST_PATH is what tells it where they are; the toolchain file supplies
# the compiler, the sysroot and the pkg-config variables Qt's dependency
# probing needs.
say "stage 2: target Qt (cross aarch64)"
if [ ! -f "$TARGET_QT/lib/libQt6Core.so" ]; then
  mkdir -p "$QT_ROOT/build-target"
  CWD="$QT_ROOT/build-target" in_env "
    set -e
    [ -f CMakeCache.txt ] || '$SRC/configure' -prefix '$TARGET_QT' -release \
      -qt-host-path '$HOST_QT' \
      -nomake examples -nomake tests \
      -no-openssl -no-icu \
      -- -DCMAKE_TOOLCHAIN_FILE='$WORK/toolchains/$TARGET_ID/toolchain.cmake' \
         -DCMAKE_BUILD_TYPE=Release
    cmake --build . --parallel $JOBS
    cmake --install ."
else
  echo "  already built"
fi
file "$TARGET_QT/lib/libQt6Core.so.$QT_VERSION" | cut -c1-90

# Stage 3. A Qt project with no cross-specific content, built by the target
# Qt's own qt-cmake.
say "stage 3: sample application"
rm -rf "$QT_ROOT/build-sample"
mkdir -p "$QT_ROOT/build-sample"
CWD="$QT_ROOT/build-sample" in_env "
  set -e
  '$TARGET_QT/bin/qt-cmake' -GNinja -DCMAKE_BUILD_TYPE=Release '$SAMPLE_SRC'
  cmake --build . --parallel $JOBS"
file "$QT_ROOT/build-sample/qt6_cross" | cut -c1-90

say "audit: the application and every Qt library it links"
CROSSFORGE="${CROSSFORGE:-$(command -v crossforge || echo ./target/release/crossforge)}"
"$CROSSFORGE" audit \
  --sysroot "$WORK/toolchains/$TARGET_ID/$TRIPLE/sysroot" --arch aarch64 \
  --allow-needed libQt6Core.so.6 \
  "$QT_ROOT/build-sample/qt6_cross" "$TARGET_QT/lib/libQt6Core.so.$QT_VERSION"

say "run on the target"
SYSROOT="$WORK/toolchains/$TARGET_ID/$TRIPLE/sysroot"
qemu-aarch64 -L "$SYSROOT" \
  -E LD_LIBRARY_PATH="$TARGET_QT/lib:$WORK/toolchains/$TARGET_ID/$TRIPLE/lib64" \
  -E QT_QPA_PLATFORM=minimal \
  "$QT_ROOT/build-sample/qt6_cross"
