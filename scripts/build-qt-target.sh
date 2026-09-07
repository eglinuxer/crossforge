#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 6 ]]; then
  echo "usage: $0 BUILD_ROOT INSTALL_ROOT ARCH TRIPLE JOBS PHASE" >&2
  exit 2
fi

build_root=$1
install_root=$2
arch=$3
triple=$4
jobs=$5
phase=$6
cmake=/opt/crossforge/host-tools/cmake/4.4.0/bin/cmake
ninja=/opt/crossforge/host-tools/ninja/1.13.2/bin/ninja
qt_host=/opt/crossforge/qualification/qt/6.8.4/host
toolchain=/opt/crossforge/targets/$triple/bin

case "$arch:$triple:$build_root:$install_root" in
  x86_64:x86_64-unknown-linux-gnu:/work/build/qt-x86_64:/opt/crossforge/qualification/qt/6.8.4/targets/x86_64-unknown-linux-gnu|\
  aarch64:aarch64-unknown-linux-gnu:/work/build/qt-aarch64:/opt/crossforge/qualification/qt/6.8.4/targets/aarch64-unknown-linux-gnu) ;;
  *) echo "error: Qt target build identity differs" >&2; exit 1 ;;
esac
[[ -d "$build_root" && -f "$build_root/CMakeCache.txt" \
  && -f "$build_root/config.summary" && ! -e "$install_root" \
  && -x "$cmake" && -x "$ninja" \
  && -x "$toolchain/$triple-g++" \
  && -f "$qt_host/qt-host-build.json" ]] || {
  echo "error: Qt target build input closure is incomplete" >&2
  exit 1
}
[[ "$jobs" =~ ^[1-9][0-9]?$ && "$jobs" -le 64 \
  && "$($toolchain/$triple-g++ -dumpmachine)" == "$triple" ]] || {
  echo "error: Qt target build tool or job identity differs" >&2
  exit 1
}
[[ -z "${HOSTRUNNER:-}" && -z "${CMAKE_CROSSCOMPILING_EMULATOR:-}" ]] || {
  echo "error: Qt target build forbids execution adapters" >&2
  exit 1
}
case "$phase" in
  webengine|complete) ;;
  *) echo "error: Qt target build phase differs" >&2; exit 1 ;;
esac

soft_limit=$(ulimit -Sn)
hard_limit=$(ulimit -Hn)
if [[ "$soft_limit" -lt 65536 ]]; then
  target_limit=65536
  if [[ "$hard_limit" != unlimited && "$hard_limit" -lt "$target_limit" ]]; then
    target_limit=$hard_limit
  fi
  ulimit -Sn "$target_limit"
fi
[[ "$(ulimit -Sn)" -ge 4096 ]] || {
  echo "error: Qt WebEngine target build requires at least 4096 open files" >&2
  exit 1
}

export PATH=/opt/crossforge/host-tools/cmake/4.4.0/bin:/opt/crossforge/host-tools/ninja/1.13.2/bin:$qt_host/bin:$toolchain:$PATH
export LD_LIBRARY_PATH=$qt_host/lib:/opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg/lib64:/opt/crossforge/qualification/qt/6.8.4/deps/host/xcb-util-cursor/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export PYTHONPATH=/usr/lib/python3.6/site-packages
export LC_ALL=C.UTF-8
export SOURCE_DATE_EPOCH=0

run_logged() {
  local label=$1
  local log=$2
  shift 2
  if /usr/libexec/platform-python /work/scripts/run-with-heartbeat.py \
      --label "$label" --interval 60 --log "$log" -- "$@"; then
    echo "$label completed; full log: $log"
    return 0
  fi
  echo "error: $label failed; reporting bounded diagnostics" >&2
  /usr/libexec/platform-python \
    /work/scripts/print-build-log-diagnostics.py "$log" >&2 || true
  return 1
}

if [[ "$phase" == webengine ]]; then
  run_logged "Qt WebEngine target build" "$build_root/webengine-build.log" \
    "$cmake" --build "$build_root" --target WebEngineCore \
      --parallel "$jobs"
  [[ -f "$build_root/qtwebengine/src/core/Release/$arch/QtWebEngineCore.stamp" ]] || {
    echo "error: Qt WebEngine target build did not create its completion stamp" >&2
    exit 1
  }
  exit 0
fi

[[ -f "$build_root/qtwebengine/src/core/Release/$arch/QtWebEngineCore.stamp" \
  && -f "$build_root/webengine-build.log" ]] || {
  echo "error: complete Qt target build requires the cached WebEngine phase" >&2
  exit 1
}
run_logged "Qt target build" "$build_root/build.log" \
  "$cmake" --build "$build_root" --parallel "$jobs"
run_logged "Qt target install" "$build_root/install.log" \
  "$cmake" --install "$build_root"
