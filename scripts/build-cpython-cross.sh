#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 10 ]]; then
  echo "usage: $0 SOURCE BUILD_DIR PREFIX SYSROOT TOOLCHAIN TARGET BUILD_PYTHON VERSION ADAPTER JOBS" >&2
  exit 2
fi

source_directory=$1
build_directory=$2
prefix=$3
sysroot=$4
toolchain=$5
target=$6
build_python=$7
version=$8
adapter=$9
jobs=${10}
minor=${version%.*}
compact_minor=${minor/./}

[[ -x "$source_directory/configure" && -x "$source_directory/config.guess" ]] || {
  echo "error: invalid CPython source: $source_directory" >&2
  exit 1
}
[[ "$version" =~ ^3\.[0-9]+\.[0-9]+$ && "$adapter" == modern ]] || {
  echo "error: unsupported CPython version/adapter: $version/$adapter" >&2
  exit 1
}
[[ -x "$build_python" ]] || {
  echo "error: matching build Python is missing: $build_python" >&2
  exit 1
}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || {
  echo "error: JOBS must be positive" >&2
  exit 1
}

case "$target" in
  x86_64-unknown-linux-gnu)
    target_arch=x86_64
    target_machine='Advanced Micro Devices X86-64'
    target_elf_machine=62
    ;;
  aarch64-unknown-linux-gnu)
    target_arch=aarch64
    target_machine=AArch64
    target_elf_machine=183
    ;;
  *)
    echo "error: unsupported Python target: $target" >&2
    exit 1
    ;;
esac

[[ "$prefix" == /opt/crossforge/python/cp"$compact_minor"/targets/"$target" ]] || {
  echo "error: target Python prefix differs from version/target" >&2
  exit 1
}
[[ "$sysroot" == /opt/crossforge/sysroots/el8/"$target_arch" ]] || {
  echo "error: target Python sysroot differs from target" >&2
  exit 1
}
[[ "$toolchain" == /opt/crossforge/targets/"$target" ]] || {
  echo "error: target Python toolchain differs from target" >&2
  exit 1
}
for tool in gcc g++ ar ranlib readelf ld nm strip objcopy; do
  [[ -x "$toolchain/bin/$target-$tool" ]] || {
    echo "error: missing cross tool: $target-$tool" >&2
    exit 1
  }
done
[[ "$($build_python -I -c 'import platform; print(platform.python_version())')" == "$version" ]] || {
  echo "error: build Python patch version differs from target source" >&2
  exit 1
}

build=$($source_directory/config.guess)
[[ "$build" != "$target" && "$build" == x86_64-*-linux-gnu ]] || {
  echo "error: CPython target must remain a real cross build" >&2
  exit 1
}

mkdir -p "$build_directory"
config_site=$build_directory/config.site
{
  printf 'ac_cv_buggy_getaddrinfo=no\n'
  printf 'ac_cv_file__dev_ptmx=yes\n'
  printf 'ac_cv_file__dev_ptc=no\n'
  printf 'ac_cv_working_tzset=yes\n'
  printf 'ac_cv_broken_sem_getvalue=no\n'
  printf 'ac_cv_posix_semaphores_enabled=yes\n'
  printf 'ac_cv_aligned_required=no\n'
  if [[ "$target_arch" == aarch64 ]]; then
    printf 'ac_cv_wchar_t_signed=no\n'
  else
    printf 'ac_cv_wchar_t_signed=yes\n'
  fi
} >"$config_site"

cd "$build_directory"
export PATH="$toolchain/bin:/usr/bin:/bin"
export CONFIG_SITE=$config_site
export CC="$toolchain/bin/$target-gcc --sysroot=$sysroot"
export CXX="$toolchain/bin/$target-g++ --sysroot=$sysroot"
export CPP="$toolchain/bin/$target-gcc --sysroot=$sysroot -E"
export AR=$toolchain/bin/$target-ar
export RANLIB=$toolchain/bin/$target-ranlib
export READELF=$toolchain/bin/$target-readelf
export LD=$toolchain/bin/$target-ld
export NM=$toolchain/bin/$target-nm
export STRIP=$toolchain/bin/$target-strip
export OBJCOPY=$toolchain/bin/$target-objcopy
export CFLAGS='-O2 -g -pipe -ffile-prefix-map=/work=/usr/src/debug/crossforge'
export CXXFLAGS=$CFLAGS
export LDFLAGS='-Wl,-z,relro,-z,now'
export PKG_CONFIG_PATH=
export PKG_CONFIG_SYSROOT_DIR=$sysroot
export PKG_CONFIG_LIBDIR=$sysroot/usr/lib64/pkgconfig:$sysroot/usr/share/pkgconfig
export SOURCE_DATE_EPOCH=0
unset HOSTRUNNER PYTHON_FOR_BUILD

# An empty HOSTRUNNER is necessary but not sufficient: same-ISA x86_64 could
# execute natively, and a build host may have global binfmt configured for
# aarch64. Interpose execve in every host process and reject target ELF files
# from the build/install roots. A target canary proves the guard is active
# before configure or make is allowed to continue.
script_directory=$(cd "$(dirname "$0")" && pwd)
auditor=$build_directory/deny-target-exec.so
auditor_log=$build_directory/target-exec-audit.log
canary=$build_directory/target-exec-canary
host_cc=/opt/rh/gcc-toolset-15/root/usr/bin/gcc
[[ -x "$host_cc" ]] || {
  echo "error: locked host compiler is missing" >&2
  exit 1
}
"$host_cc" -shared -fPIC -O2 -Wall -Wextra -Werror \
  "$script_directory/deny-target-exec.c" -ldl -o "$auditor"
"$toolchain/bin/$target-gcc" --sysroot="$sysroot" \
  "$script_directory/target-exec-canary.c" -o "$canary"
: >"$auditor_log"
export CROSSFORGE_DENY_EXEC_ROOTS="$build_directory:$prefix"
export CROSSFORGE_DENY_EXEC_MACHINE=$target_elf_machine
export CROSSFORGE_DENY_EXEC_LOG=$auditor_log
export LD_PRELOAD=$auditor
if /bin/bash -c '"$1"' crossforge-target-guard "$canary" >/dev/null 2>&1; then
  echo "error: target-execution guard did not reject its canary" >&2
  exit 1
fi
grep -Fx "$canary" "$auditor_log" >/dev/null || {
  echo "error: target-execution guard produced no canary evidence" >&2
  exit 1
}

"$source_directory/configure" \
  --build="$build" \
  --host="$target" \
  --prefix="$prefix" \
  --with-build-python="$build_python" \
  --with-pkg-config=yes \
  --with-computed-gotos=yes \
  --with-ensurepip=no \
  --disable-test-modules

grep -F "PYTHON_FOR_BUILD=" Makefile | grep -F "$build_python" >/dev/null || {
  echo "error: Makefile does not use the matching build Python" >&2
  exit 1
}
grep -Eq '^HOSTRUNNER=[[:space:]]*$' Makefile || {
  echo "error: target execution leaked into the cross-build stage" >&2
  exit 1
}

make -j"$jobs"
make install
unset LD_PRELOAD CROSSFORGE_DENY_EXEC_ROOTS CROSSFORGE_DENY_EXEC_MACHINE \
  CROSSFORGE_DENY_EXEC_LOG

python=$prefix/bin/python$minor
[[ -x "$python" ]] || {
  echo "error: target Python was not installed" >&2
  exit 1
}
mapfile -t sysconfig_data < <(
  find "$prefix/lib/python$minor" -name '_sysconfigdata_*.py' -type f -print
)
[[ ${#sysconfig_data[@]} -eq 1 ]] || {
  echo "error: target SDK must contain exactly one sysconfigdata module" >&2
  exit 1
}
[[ -f "$prefix/include/python$minor/pyconfig.h" ]] || {
  echo "error: target SDK is missing pyconfig.h" >&2
  exit 1
}
if grep -Eq '^#define[[:space:]]+HAVE_ALIGNED_REQUIRED[[:space:]]+1$' \
    "$prefix/include/python$minor/pyconfig.h"; then
  echo "error: target Python incorrectly requires aligned memory access" >&2
  exit 1
fi

for module in _bz2 _ctypes _hashlib _lzma _sqlite3 _ssl _uuid zlib; do
  mapfile -t matches < <(
    find "$prefix/lib/python$minor/lib-dynload" \
      -maxdepth 1 -name "$module.*.so" -type f -print
  )
  [[ ${#matches[@]} -eq 1 ]] || {
    echo "error: required target module $module was not installed exactly once" >&2
    exit 1
  }
done

headers=$($READELF -h "$python")
grep -F "Machine:" <<<"$headers" | grep -F "$target_machine" >/dev/null || {
  echo "error: target Python has the wrong ELF machine" >&2
  exit 1
}

echo "built cross CPython $version for $target at $prefix"
