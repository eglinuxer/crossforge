#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 8 ]]; then
  echo "usage: $0 IDENTITY ARCHIVE BUILD_ROOT DESTINATION PREFIX SYSROOT TOOLCHAIN JOBS" >&2
  exit 2
fi

identity=$1
archive=$2
build_root=$3
destination=$4
prefix=$5
sysroot=$6
toolchain=$7
jobs=$8
source_root=$build_root/source

[[ "$archive" = /* && "$build_root" = /* && "$destination" = /* \
  && "$prefix" = /* && "$toolchain" = /* ]] || {
  echo "error: FFmpeg build paths must be absolute" >&2
  exit 1
}
[[ -f "$archive" && ! -e "$build_root" && ! -e "$destination" ]] || {
  echo "error: FFmpeg input is missing or an output path is stale" >&2
  exit 1
}
[[ "$jobs" =~ ^[1-9][0-9]?$ && "$jobs" -le 64 ]] || {
  echo "error: FFmpeg job count must be between 1 and 64" >&2
  exit 1
}

mkdir -p "$source_root" "$destination"
tar --extract --xz --file "$archive" --directory "$source_root" \
  --strip-components=1 --no-same-owner --no-same-permissions
[[ -x "$source_root/configure" ]] || chmod 0755 "$source_root/configure"

common=(
  "--prefix=$prefix"
  "--libdir=$prefix/lib64"
  "--shlibdir=$prefix/lib64"
  "--incdir=$prefix/include"
  --disable-static
  --enable-shared
  --enable-pic
  --disable-programs
  --disable-avdevice
  --disable-avfilter
  --disable-doc
  --disable-debug
  --disable-stripping
  --disable-autodetect
  --disable-gpl
  --disable-version3
  --disable-nonfree
  --enable-network
  --enable-pthreads
  --enable-openssl
  --enable-zlib
  --extra-cflags="-O2 -g0 -fPIC -ffile-prefix-map=$build_root=/usr/src/debug/ffmpeg"
)

export LC_ALL=C
export SOURCE_DATE_EPOCH=0
if [[ "$identity" == host ]]; then
  [[ -z "$sysroot" && "$prefix" == "$destination" ]] || {
    echo "error: FFmpeg host path contract differs" >&2
    exit 1
  }
  cc=$toolchain/gcc
  cxx=$toolchain/g++
  ar=$toolchain/ar
  nm=$toolchain/nm
  ranlib=$toolchain/ranlib
  strip=$toolchain/strip
  [[ "$($cc -dumpfullversion)" == 15.2.1 ]] || {
    echo "error: FFmpeg host compiler version differs" >&2
    exit 1
  }
  configure=(
    "${common[@]}"
    "--cc=$cc"
    "--cxx=$cxx"
    "--ar=$ar"
    "--nm=$nm"
    "--ranlib=$ranlib"
    "--strip=$strip"
    --x86asmexe=yasm
  )
  install=(make install-libs install-headers)
else
  case "$identity" in
    x86_64-unknown-linux-gnu) architecture=x86_64 ;;
    aarch64-unknown-linux-gnu) architecture=aarch64 ;;
    *) echo "error: unsupported FFmpeg target identity" >&2; exit 1 ;;
  esac
  [[ "$sysroot" = /* && -d "$sysroot" && "$prefix" == /usr ]] || {
    echo "error: FFmpeg target path contract differs" >&2
    exit 1
  }
  cross_prefix=$toolchain/$identity-
  cc=${cross_prefix}gcc
  [[ "$($cc -dumpmachine)" == "$identity" ]] || {
    echo "error: FFmpeg cross compiler identity differs" >&2
    exit 1
  }
  export PKG_CONFIG_SYSROOT_DIR=$sysroot
  export PKG_CONFIG_LIBDIR=$sysroot/usr/lib64/pkgconfig:$sysroot/usr/share/pkgconfig
  configure=(
    "${common[@]}"
    --enable-cross-compile
    --target-os=linux
    "--arch=$architecture"
    "--cross-prefix=$cross_prefix"
    "--sysroot=$sysroot"
    --pkg-config=pkg-config
  )
  if [[ "$architecture" == x86_64 ]]; then
    configure+=(--x86asmexe=yasm)
  fi
  install=(make "DESTDIR=$destination" install-libs install-headers)
fi

cd "$source_root"
./configure "${configure[@]}" 2>&1 | tee "$build_root/configure.log"
make -j"$jobs" 2>&1 | tee "$build_root/build.log"
"${install[@]}" 2>&1 | tee "$build_root/install.log"

install_root=$destination
if [[ "$identity" != host ]]; then
  install_root=$destination/usr
fi
for library in avcodec avformat avutil swresample swscale; do
  [[ -e "$install_root/lib64/lib$library.so" ]] || {
    echo "error: FFmpeg did not install lib$library" >&2
    exit 1
  }
done
install -D -m 0644 "$source_root/COPYING.LGPLv2.1" \
  "$install_root/share/licenses/ffmpeg/COPYING.LGPLv2.1"
