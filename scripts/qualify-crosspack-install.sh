#!/bin/sh
set -eu

if [ "$#" -ne 4 ]; then
  echo "usage: qualify-crosspack-install.sh FORMAT ARCH PACKAGE_ROOT OUTPUT" >&2
  exit 2
fi

format=$1
arch=$2
package_root=$3
output=$4
install_root="/tmp/crosspack-install-$format-$arch"

test "$arch" = x86_64 || test "$arch" = aarch64
test -d "$package_root/packages"
test -f "$package_root/installed.sha256"
test ! -e "$install_root"
mkdir -p "$install_root"

case "$format" in
  deb)
    expected_arch=amd64
    if [ "$arch" = aarch64 ]; then
      expected_arch=arm64
    fi
    dpkg --root="$install_root" --force-architecture \
      --install "$package_root"/packages/*.deb
    metadata=$(dpkg-query --admindir="$install_root/var/lib/dpkg" \
      --show --showformat='${Package} ${Version} ${Architecture} ${Status}\n')
    ;;
  rpm)
    expected_arch=$arch
    rpm --root "$install_root" --initdb
    rpm --root "$install_root" --ignorearch --nodeps \
      --install "$package_root"/packages/*.rpm
    metadata=$(rpm --root "$install_root" --query --all \
      --queryformat '%{NAME} %{VERSION}-%{RELEASE} %{ARCH} installed ok\n')
    ;;
  *)
    echo "unsupported package format: $format" >&2
    exit 2
    ;;
esac

count=$(printf '%s\n' "$metadata" | sed '/^$/d' | wc -l)
test "$count" -eq 4
printf '%s\n' "$metadata" | grep -F "crossforge-demo 1.2.3-4 $expected_arch"
printf '%s\n' "$metadata" | grep -F "crossforge-demo-dev 1.2.3-4 $expected_arch" \
  || printf '%s\n' "$metadata" | grep -F "crossforge-demo-devel 1.2.3-4 $expected_arch"
printf '%s\n' "$metadata" | grep -F "crossforge-demo-tools 1.2.3-4 $expected_arch"
if [ "$format" = deb ]; then
  printf '%s\n' "$metadata" | grep -F \
    "crossforge-demo-dbgsym 1.2.3-4 $expected_arch"
else
  printf '%s\n' "$metadata" | grep -F \
    "crossforge-demo-debuginfo 1.2.3-4 $expected_arch"
fi

(
  cd "$install_root"
  sha256sum --check "$package_root/installed.sha256"
)
test "$(readlink "$install_root/usr/lib64/libcrossforge-demo.so")" \
  = libcrossforge-demo.so.1
test -x "$install_root/usr/bin/crossforge-demo"
test -f "$install_root/usr/include/crossforge/demo.h"
test -f "$install_root/usr/share/crossforge/README"
test -f \
  "$install_root/usr/lib/debug/usr/lib64/libcrossforge-demo.so.1.debug"

mkdir -p "$(dirname "$output")"
printf 'crosspack-install-v1 %s %s passed\n' "$format" "$arch" >"$output"
