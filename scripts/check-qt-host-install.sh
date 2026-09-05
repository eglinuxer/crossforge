#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 PREFIX" >&2
  exit 2
fi

prefix=$1
[[ "$prefix" == /opt/crossforge/qualification/qt/6.8.4/host ]] || {
  echo "error: Qt host install prefix differs" >&2
  exit 1
}

for relative in \
  bin/assistant \
  bin/designer \
  bin/lrelease \
  bin/lupdate \
  bin/qmake6 \
  bin/qt-cmake \
  bin/qt-configure-module \
  bin/qtpaths6 \
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
    echo "error: Qt host install is missing $relative" >&2
    exit 1
  }
done
