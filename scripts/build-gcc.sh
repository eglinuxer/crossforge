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
  echo "error: invalid prepared GCC source: $source_directory" >&2
  exit 1
}
[[ -x "$prefix/bin/$target-ld" ]] || {
  echo "error: cross binutils are not installed at $prefix" >&2
  exit 1
}
[[ -f "$sysroot/usr/include/features.h" && -f "$sysroot/usr/lib64/crt1.o" ]] || {
  echo "error: incomplete target sysroot: $sysroot" >&2
  exit 1
}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || {
  echo "error: JOBS must be positive" >&2
  exit 1
}

target_options=()
case "$target" in
  x86_64-unknown-linux-gnu)
    target_options=(--enable-cet --with-arch=x86-64 --with-tune=generic)
    target_cflags='-O2 -g -pipe -march=x86-64 -mtune=generic -D_GLIBCXX_ASSERTIONS -ffile-prefix-map=/work=/usr/src/debug/crossforge'
    ;;
  aarch64-unknown-linux-gnu)
    target_options=(--with-arch=armv8-a --with-tune=generic)
    target_cflags='-O2 -g -pipe -march=armv8-a -mtune=generic -D_GLIBCXX_ASSERTIONS -ffile-prefix-map=/work=/usr/src/debug/crossforge'
    ;;
  *)
    echo "error: unsupported target: $target" >&2
    exit 1
    ;;
esac

build=$($source_directory/config.guess)
[[ "$build" != "$target" ]] || {
  echo "error: target must remain distinct from the canonical build triple" >&2
  exit 1
}
isl_prefix=$build_directory/isl-install
mkdir -p "$build_directory/isl-build" "$isl_prefix" "$destdir"

if [[ ! -f "$isl_prefix/lib/libisl.a" ]]; then
  (
    cd "$build_directory/isl-build"
    "$source_directory/isl-0.24/configure" \
      --prefix="$isl_prefix" \
      --disable-shared \
      --enable-static
    make -j"$jobs"
    make install
  )
fi

mkdir -p "$build_directory/gcc-build"
cd "$build_directory/gcc-build"
export PATH="$prefix/bin:$PATH"
export CFLAGS_FOR_TARGET=$target_cflags
export CXXFLAGS_FOR_TARGET=$CFLAGS_FOR_TARGET
export LDFLAGS_FOR_TARGET='-Wl,-z,relro,-z,now'

"$source_directory/configure" \
  --build="$build" \
  --host="$build" \
  --target="$target" \
  --prefix="$prefix" \
  --with-sysroot="$sysroot" \
  --with-build-sysroot="$sysroot" \
  --with-native-system-header-dir=/usr/include \
  --with-isl="$isl_prefix" \
  --with-system-zlib \
  --with-gcc-major-version-only \
  --enable-languages=c,c++,lto \
  --enable-shared \
  --enable-threads=posix \
  --enable-checking=release \
  --enable-plugin \
  --enable-__cxa_atexit \
  --enable-gnu-unique-object \
  --enable-linker-build-id \
  --enable-initfini-array \
  --with-linker-hash-style=gnu \
  --disable-bootstrap \
  --disable-analyzer \
  --disable-multilib \
  --disable-nls \
  --disable-libsanitizer \
  --disable-libvtv \
  --disable-libstdcxx-pch \
  --disable-libquadmath \
  "${target_options[@]}"

make -j"$jobs" all-gcc all-target-libgcc all-target-libstdc++-v3
make DESTDIR="$destdir" \
  install-gcc install-target-libgcc install-target-libstdc++-v3

[[ -x "$destdir$prefix/bin/$target-gcc" ]] || {
  echo "error: GCC install did not produce $target-gcc" >&2
  exit 1
}
[[ -f "$build_directory/gcc-build/$target/libstdc++-v3/src/.libs/libstdc++_nonshared80.a" ]] || {
  echo "error: vendor libstdc++_nonshared80.a was not built" >&2
  exit 1
}

echo "built GCC for $target"
