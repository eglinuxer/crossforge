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

mkdir -p "$install_root/bin"
cp -L /bin/sh "$install_root/bin/sh"
ldd /bin/sh | awk '{
  for (field = 1; field <= NF; field++) {
    if ($field ~ /^\//) print $field
  }
}' | sort -u | while IFS= read -r dependency; do
  cp --parents "$dependency" "$install_root"
done

case "$format" in
  deb)
    expected_arch=amd64
    expected_independent_arch=all
    if [ "$arch" = aarch64 ]; then
      expected_arch=arm64
    fi
    dpkg --root="$install_root" --force-architecture \
      --install "$package_root"/packages/*.deb
    metadata=$(dpkg-query --admindir="$install_root/var/lib/dpkg" \
      --show --showformat='${Package} ${Version} ${Architecture} ${Status}\n')
    relation_metadata=$(dpkg-query \
      --admindir="$install_root/var/lib/dpkg" \
      --show --showformat='${Provides}\n' crossforge-demo)
    config_metadata=$(dpkg-query \
      --admindir="$install_root/var/lib/dpkg" \
      --show --showformat='${Conffiles}\n' crossforge-demo)
    description_metadata=$(dpkg-query \
      --admindir="$install_root/var/lib/dpkg" \
      --show --showformat='${Description}\n' crossforge-demo)
    summary_metadata=$(printf '%s\n' "$description_metadata" | sed -n '1p')
    ;;
  rpm)
    expected_arch=$arch
    expected_independent_arch=noarch
    rpm --root "$install_root" --initdb
    rpm --root "$install_root" --ignorearch --nodeps \
      --install "$package_root"/packages/*.rpm
    metadata=$(rpm --root "$install_root" --query --all \
      --queryformat '%{NAME} %{VERSION}-%{RELEASE} %{ARCH} installed ok\n')
    relation_metadata=$(rpm --root "$install_root" --query \
      --provides crossforge-demo)
    config_metadata=$(rpm --root "$install_root" --query \
      --configfiles crossforge-demo)
    summary_metadata=$(rpm --root "$install_root" --query \
      --queryformat '%{SUMMARY}\n' crossforge-demo)
    description_metadata=$(rpm --root "$install_root" --query \
      --queryformat '%{DESCRIPTION}\n' crossforge-demo)
    license_metadata=$(rpm --root "$install_root" --query \
      --queryformat '%{LICENSE}\n' crossforge-demo)
    test "$license_metadata" = 'MIT OR Apache-2.0'
    ;;
  *)
    echo "unsupported package format: $format" >&2
    exit 2
    ;;
esac

printf '%s\n' "$relation_metadata" | grep -F crossforge-demo-virtual
printf '%s\n' "$config_metadata" | grep -F /etc/crossforge-demo.conf
test "$summary_metadata" = 'Crosspack runtime fixture'
printf '%s\n' "$description_metadata" | grep -F \
  'Runtime files used to qualify Crosspack packages.'
printf '%s\n' "$description_metadata" | grep -F \
  'Includes a shared library, command, configuration, and state directory.'

count=$(printf '%s\n' "$metadata" | sed '/^$/d' | wc -l)
test "$count" -eq 4
printf '%s\n' "$metadata" | grep -F "crossforge-demo 1.2.3-4 $expected_arch"
printf '%s\n' "$metadata" | grep -F "crossforge-demo-dev 1.2.3-4 $expected_arch" \
  || printf '%s\n' "$metadata" | grep -F "crossforge-demo-devel 1.2.3-4 $expected_arch"
printf '%s\n' "$metadata" | grep -F \
  "crossforge-demo-tools 1.2.3-4 $expected_independent_arch"
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
test "$(stat -c '%a %U %G' "$install_root/etc/crossforge-demo.conf")" \
  = '640 root root'
test -f "$install_root/usr/include/crossforge/demo.h"
test -f "$install_root/usr/share/crossforge/README"
test -f \
  "$install_root/usr/lib/debug/usr/lib64/libcrossforge-demo.so.1.debug"
script_log="$install_root/crossforge-scriptlets.log"
test "$(sed -n '1p' "$script_log")" = "crossforge-$format-pre-install"
test "$(sed -n '2p' "$script_log")" = "crossforge-$format-post-install"

printf '%s\n' 'mode=user' > "$install_root/etc/crossforge-demo.conf"
case "$format" in
  deb)
    dpkg --root="$install_root" --force-architecture --force-confold \
      --install "$package_root"/upgrade/packages/*.deb
    upgraded=$(dpkg-query --admindir="$install_root/var/lib/dpkg" \
      --show --showformat='${Package} ${Version} ${Architecture} ${Status}\n')
    alternate="$install_root/etc/crossforge-demo.conf.dpkg-dist"
    ;;
  rpm)
    rpm --root "$install_root" --ignorearch --nodeps \
      --upgrade "$package_root"/upgrade/packages/*.rpm
    upgraded=$(rpm --root "$install_root" --query --all \
      --queryformat '%{NAME} %{VERSION}-%{RELEASE} %{ARCH} installed ok\n')
    alternate="$install_root/etc/crossforge-demo.conf.rpmnew"
    ;;
esac
printf '%s\n' "$upgraded" | grep -F \
  "crossforge-demo 1.2.3-5 $expected_arch"
grep -Fx 'mode=user' "$install_root/etc/crossforge-demo.conf"
grep -Fx 'mode=upgrade-default' "$alternate"
for lifecycle in pre-install post-install pre-remove post-remove; do
  grep -Fx "crossforge-$format-$lifecycle" "$script_log"
done

case "$format" in
  deb)
    dpkg --root="$install_root" --force-depends --remove crossforge-demo
    ;;
  rpm)
    rpm --root "$install_root" --nodeps --erase crossforge-demo
    ;;
esac
test "$(grep -Fxc "crossforge-$format-pre-remove" "$script_log")" -ge 2
test "$(grep -Fxc "crossforge-$format-post-remove" "$script_log")" -ge 2

mkdir -p "$(dirname "$output")"
printf 'crosspack-install-v2 %s %s passed\n' "$format" "$arch" >"$output"
