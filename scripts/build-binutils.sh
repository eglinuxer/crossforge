#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 7 ]]; then
  echo "usage: $0 SOURCE BUILD_DIR DESTDIR PREFIX SYSROOT TARGET JOBS" >&2
  exit 2
fi

source_directory=$1
build_directory=$2
destdir=$3
prefix=$4
sysroot=$5
target=$6
jobs=$7

[[ -x "$source_directory/configure" && -x "$source_directory/config.guess" ]] || {
  echo "error: invalid prepared binutils source: $source_directory" >&2
  exit 1
}
[[ "$prefix" == /opt/crossforge/targets/"$target" ]] || {
  echo "error: prefix and target disagree" >&2
  exit 1
}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || {
  echo "error: JOBS must be positive" >&2
  exit 1
}

case "$target" in
  x86_64-unknown-linux-gnu)
    target_arch=x86_64
    ;;
  aarch64-unknown-linux-gnu)
    target_arch=aarch64
    ;;
  *)
    echo "error: unsupported target: $target" >&2
    exit 1
    ;;
esac
[[ "$sysroot" == /opt/crossforge/sysroots/el8/"$target_arch" ]] || {
  echo "error: sysroot and target disagree" >&2
  exit 1
}
grep -Fx "prep_target_arch=$target_arch" \
  "$source_directory/.crossforge/preparation.txt" >/dev/null || {
  echo "error: prepared binutils source architecture does not match $target_arch" >&2
  exit 1
}

build=$($source_directory/config.guess)
[[ "$build" != "$target" ]] || {
  echo "error: target must remain distinct from the canonical build triple" >&2
  exit 1
}
mkdir -p "$build_directory" "$destdir"
cd "$build_directory"

CFLAGS='-O2 -g -pipe' \
CXXFLAGS='-O2 -g -pipe' \
  "$source_directory/configure" \
    --build="$build" \
    --host="$build" \
    --target="$target" \
    --prefix="$prefix" \
    --program-prefix="$target-" \
    --with-sysroot="$sysroot" \
    --with-system-zlib \
    --enable-plugins \
    --enable-lto \
    --enable-default-hash-style=gnu \
    --enable-deterministic-archives \
    --enable-relro=yes \
    --enable-new-dtags \
    --disable-rpath \
    --enable-separate-code=yes \
    --enable-rosegment=yes \
    --enable-warn-execstack=yes \
    --enable-default-execstack=no \
    --enable-warn-rwx-segments=yes \
    --disable-gold \
    --disable-gprofng \
    --disable-nls \
    --disable-werror \
    --disable-shared \
    --without-debuginfod

make -j"$jobs" MAKEINFO=true
make DESTDIR="$destdir" MAKEINFO=true install

for tool in ar as ld nm objcopy objdump ranlib readelf strip; do
  installed=$destdir$prefix/bin/$target-$tool
  [[ -x "$installed" ]] || {
    echo "error: binutils install is missing $installed" >&2
    exit 1
  }
done

echo "built binutils for $target"
