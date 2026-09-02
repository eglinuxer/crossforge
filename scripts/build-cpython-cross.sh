#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 9 ]]; then
  echo "usage: $0 SOURCE BUILD_DIR PREFIX SYSROOT TOOLCHAIN TARGET BUILD_PYTHON VERSION JOBS" >&2
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
jobs=$9
minor=${version%.*}
compact_minor=${minor/./}
script_directory=$(cd "$(dirname "$0")" && pwd)

[[ -x "$source_directory/configure" && -x "$source_directory/config.guess" ]] || {
  echo "error: invalid CPython source: $source_directory" >&2
  exit 1
}
expected_build_python=/opt/crossforge/python/cp"$compact_minor"/build/bin/python"$minor"
[[ "$build_python" == "$expected_build_python" ]] || {
  echo "error: build Python path differs from version" >&2
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
[[ ! -e "$build_directory" && ! -L "$build_directory" ]] || {
  echo "error: refusing stale cross CPython build directory: $build_directory" >&2
  exit 1
}
[[ ! -e "$prefix" && ! -L "$prefix" ]] || {
  echo "error: refusing stale target Python prefix: $prefix" >&2
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
while IFS='=' read -r name unused; do
  case "$name" in
    PYTHON*|_PYTHON*|ac_cv_*) unset "$name" ;;
  esac
done < <(env)
unset AR AS BLDSHARED CCSHARED CFLAGS CONFIG_SITE CPATH CPP CPPFLAGS CC \
  CXX CXXFLAGS C_INCLUDE_PATH CPLUS_INCLUDE_PATH HOSTRUNNER LD LDFLAGS \
  LDLIBS LD_LIBRARY_PATH LD_PRELOAD LIBRARY_PATH LIBS LINKFORSHARED \
  LDSHARED MAKEFLAGS MFLAGS NM OBJC_INCLUDE_PATH OBJDUMP OBJCOPY \
  PKG_CONFIG PKG_CONFIG_LIBDIR PKG_CONFIG_PATH PKG_CONFIG_SYSROOT_DIR \
  PYTHON_FOR_BUILD RANLIB READELF STRIP
[[ "$($build_python -B -I -c 'import platform; print(platform.python_version())')" == "$version" ]] || {
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
export PKG_CONFIG=/usr/bin/pkg-config
export PKG_CONFIG_SYSROOT_DIR=$sysroot
export PKG_CONFIG_LIBDIR=$sysroot/usr/lib64/pkgconfig:$sysroot/usr/share/pkgconfig
export SOURCE_DATE_EPOCH=0
unset HOSTRUNNER MAKEFLAGS MFLAGS PYTHON_FOR_BUILD
export PYTHONDONTWRITEBYTECODE=1

# An empty HOSTRUNNER is necessary but not sufficient: same-ISA x86_64 could
# execute natively, and a build host may have global binfmt configured for
# aarch64. Interpose the dynamic libc exec/spawn family and loader entry points
# in host processes, rejecting target ELF files from the build/install roots.
# This is an auditable policy guard, not a sandbox: LD_PRELOAD cannot cover
# direct system calls or statically linked programs.
auditor=$build_directory/deny-target-artifact.so
auditor_log=$build_directory/target-artifact-audit.log
canary=$build_directory/target-exec-canary
dlopen_canary=$build_directory/target-dlopen-canary.so
helper_directory=$(mktemp -d /tmp/crossforge-target-canary.XXXXXX)
artifact_helper=$helper_directory/helper
cleanup() {
  rm -f "$artifact_helper"
  rmdir "$helper_directory" 2>/dev/null || true
}
trap cleanup EXIT
host_cc=/opt/rh/gcc-toolset-15/root/usr/bin/gcc
[[ -x "$host_cc" ]] || {
  echo "error: locked host compiler is missing" >&2
  exit 1
}
"$host_cc" -shared -fPIC -O2 -Wall -Wextra -Werror \
  "$script_directory/deny-target-exec.c" -ldl -o "$auditor"
"$host_cc" -O2 -Wall -Wextra -Werror \
  "$script_directory/target-artifact-canary.c" -ldl -o "$artifact_helper"
"$toolchain/bin/$target-gcc" --sysroot="$sysroot" \
  "$script_directory/target-exec-canary.c" -o "$canary"
"$toolchain/bin/$target-gcc" --sysroot="$sysroot" -shared -fPIC \
  "$script_directory/target-exec-canary.c" -o "$dlopen_canary"
: >"$auditor_log"
export CROSSFORGE_DENY_EXEC_ROOTS="$build_directory:$prefix"
export CROSSFORGE_DENY_EXEC_MACHINE=$target_elf_machine
export CROSSFORGE_DENY_EXEC_LOG=$auditor_log
export LD_PRELOAD=$auditor

if /bin/bash -c '"$1"' crossforge-target-guard "$canary" >/dev/null 2>&1; then
  echo "error: bash execve bypassed the target-artifact guard" >&2
  exit 1
fi
grep -Fqx $'execve\t'"$canary" "$auditor_log" || {
  echo "error: bash execve produced no target-artifact audit evidence" >&2
  exit 1
}

if "$build_python" -I -c \
    'import os,sys; os.execv(sys.argv[1], [sys.argv[1]])' \
    "$canary" >/dev/null 2>&1; then
  echo "error: Python os.execv bypassed the target-artifact guard" >&2
  exit 1
fi
grep -Fqx $'execv\t'"$canary" "$auditor_log" || {
  echo "error: Python os.execv produced no target-artifact audit evidence" >&2
  exit 1
}

if "$build_python" -I -c '
import os, sys
try:
    child = os.posix_spawn(sys.argv[1], [sys.argv[1]], os.environ)
except OSError:
    raise SystemExit(77)
_, status = os.waitpid(child, 0)
raise SystemExit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 79)
' "$canary" >/dev/null 2>&1; then
  echo "error: Python os.posix_spawn bypassed the target-artifact guard" >&2
  exit 1
fi
grep -Fqx $'posix_spawn\t'"$canary" "$auditor_log" || {
  echo "error: Python os.posix_spawn produced no target-artifact audit evidence" >&2
  exit 1
}

for operation in execvp execvpe execl execlp execle fexecve execveat posix_spawnp; do
  if "$artifact_helper" "$operation" "$canary" >/dev/null 2>&1; then
    echo "error: $operation bypassed the target-artifact guard" >&2
    exit 1
  fi
  grep -Fqx "$operation"$'\t'"$canary" "$auditor_log" || {
    echo "error: $operation produced no target-artifact audit evidence" >&2
    exit 1
  }
done
for operation in dlopen dlmopen; do
  if "$artifact_helper" "$operation" "$dlopen_canary" >/dev/null 2>&1; then
    echo "error: $operation bypassed the target-artifact guard" >&2
    exit 1
  fi
  grep -Fqx "$operation"$'\t'"$dlopen_canary" "$auditor_log" || {
    echo "error: $operation produced no target-artifact audit evidence" >&2
    exit 1
  }
done

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
grep -F 'PYTHON_FOR_BUILD=' Makefile \
  | grep -F 'PYTHONPATH=$(srcdir)/Lib' \
  | grep -F '_PYTHON_SYSCONFIGDATA_PATH=' >/dev/null || {
  echo "error: build Python is not isolated from target extension modules" >&2
  exit 1
}

make -j"$jobs"
make install

exec_operations=(
  execve execv execvp execvpe execl execlp execle fexecve execveat
  posix_spawn posix_spawnp
)
loader_operations=(dlopen dlmopen)
all_operations=("${exec_operations[@]}" "${loader_operations[@]}")

for operation in "${exec_operations[@]}"; do
  count=$(awk -F '\t' -v operation="$operation" -v path="$canary" \
    '$1 == operation && $2 == path { count++ } END { print count + 0 }' \
    "$auditor_log")
  [[ "$count" -eq 1 ]] || {
    echo "error: $operation canary audit count differs: $count" >&2
    exit 1
  }
done
for operation in "${loader_operations[@]}"; do
  count=$(awk -F '\t' -v operation="$operation" -v path="$dlopen_canary" \
    '$1 == operation && $2 == path { count++ } END { print count + 0 }' \
    "$auditor_log")
  [[ "$count" -eq 1 ]] || {
    echo "error: $operation canary audit count differs: $count" >&2
    exit 1
  }
done

while IFS=$'\t' read -r kind denied extra; do
  [[ " ${all_operations[*]} " == *" $kind "* \
      && "$denied" == /* && -z "$extra" ]] || {
    echo "error: malformed target-artifact audit record" >&2
    exit 1
  }
  if [[ " ${loader_operations[*]} " == *" $kind "* ]]; then
    [[ "$denied" == "$dlopen_canary" ]] || {
      echo "error: build attempted to load target artifact: $kind $denied" >&2
      exit 1
    }
  else
    [[ "$denied" == "$canary" || "${denied##*/}" == conftest* ]] || {
      echo "error: build attempted to execute target artifact: $kind $denied" >&2
      exit 1
    }
  fi
done <"$auditor_log"
unset LD_PRELOAD CROSSFORGE_DENY_EXEC_ROOTS CROSSFORGE_DENY_EXEC_MACHINE \
  CROSSFORGE_DENY_EXEC_LOG
cleanup
trap - EXIT

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
