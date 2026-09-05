#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 INSTALL_ROOT ARCH TRIPLE" >&2
  exit 2
fi

install_root=$1
arch=$2
triple=$3
case "$arch:$triple:$install_root" in
  x86_64:x86_64-unknown-linux-gnu:/opt/crossforge/qualification/qt/6.8.4/targets/x86_64-unknown-linux-gnu|\
  aarch64:aarch64-unknown-linux-gnu:/opt/crossforge/qualification/qt/6.8.4/targets/aarch64-unknown-linux-gnu) ;;
  *) echo "error: Qt target install identity differs" >&2; exit 1 ;;
esac

prefix=$install_root/usr
for relative in \
  lib/cmake/Qt6/Qt6Config.cmake \
  lib/libQt6Core.so.6.8.4 \
  lib/libQt6Gui.so.6.8.4 \
  lib/libQt6Multimedia.so.6.8.4 \
  lib/libQt6Qml.so.6.8.4 \
  lib/libQt6Quick3D.so.6.8.4 \
  lib/libQt6WaylandClient.so.6.8.4 \
  lib/libQt6WebEngineCore.so.6.8.4 \
  libexec/QtWebEngineProcess \
  plugins/platforms/libqoffscreen.so \
  plugins/platforms/libqxcb.so \
  resources/qtwebengine_resources.pak; do
  [[ -e "$prefix/$relative" ]] || {
    echo "error: Qt target install is missing $relative" >&2
    exit 1
  }
done
