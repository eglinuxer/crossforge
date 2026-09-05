#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 BUILD_ROOT PREFIX CONFIGURE_EVIDENCE JOBS" >&2
  exit 2
fi

build_root=$1
prefix=$2
configure_evidence=$3
jobs=$4
cmake=/opt/crossforge/host-tools/cmake/4.4.0/bin/cmake
ninja=/opt/crossforge/host-tools/ninja/1.13.2/bin/ninja
cxx=/opt/rh/gcc-toolset-15/root/usr/bin/g++
ffmpeg=/opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg
xcb_cursor=/opt/crossforge/qualification/qt/6.8.4/deps/host/xcb-util-cursor

[[ "$build_root" == /work/build/qt-host \
  && "$prefix" == /opt/crossforge/qualification/qt/6.8.4/host \
  && "$configure_evidence" == "$build_root/qt-host-configure.json" ]] || {
  echo "error: Qt host build path contract differs" >&2
  exit 1
}
[[ -d "$build_root" && -f "$configure_evidence" \
  && -x "$cmake" && -x "$ninja" && ! -e "$prefix" ]] || {
  echo "error: Qt host build input is incomplete or install prefix is stale" >&2
  exit 1
}
[[ "$jobs" =~ ^[1-9][0-9]?$ && "$jobs" -le 64 ]] || {
  echo "error: Qt host job count must be between 1 and 64" >&2
  exit 1
}
atomic_library=$("$cxx" -print-file-name=libatomic.so)
[[ "$atomic_library" == /opt/rh/gcc-toolset-15/root/usr/lib/gcc/*/15/libatomic.so \
  && -f "$atomic_library" ]] || {
  echo "error: GCC Toolset 15 libatomic development library is unavailable" >&2
  exit 1
}

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
  echo "error: Qt WebEngine requires at least 4096 open files" >&2
  exit 1
}

export PATH=/opt/crossforge/host-tools/cmake/4.4.0/bin:/opt/crossforge/host-tools/ninja/1.13.2/bin:/opt/rh/gcc-toolset-15/root/usr/bin:$PATH
export LD_LIBRARY_PATH=$ffmpeg/lib64:$xcb_cursor/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export PYTHONPATH=/usr/lib/python3.6/site-packages
export LC_ALL=C.UTF-8
export SOURCE_DATE_EPOCH=0

"$cmake" --build "$build_root" --parallel "$jobs" \
  2>&1 | tee "$build_root/build.log"
"$cmake" --install "$build_root" \
  2>&1 | tee "$build_root/install.log"
