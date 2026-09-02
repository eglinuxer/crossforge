#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 10 ]]; then
  echo "usage: $0 SOURCE BUILD_DIR PREFIX SYSROOT TOOLCHAIN TARGET BUILD_PYTHON VERSION ZSTD_DEPS JOBS" >&2
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
zstd_directory=$9
jobs=${10}
minor=${version%.*}
compact_minor=${minor/./}
script_directory=$(cd "$(dirname "$0")" && pwd)
legacy_setup_build=0
if [[ "$minor" == 3.10 ]]; then
  legacy_setup_build=1
fi

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
[[ "$zstd_directory" == /work/deps/zstd && -d "$zstd_directory" \
    && ! -L "$zstd_directory" ]] || {
  echo "error: invalid cross CPython zstd dependency context" >&2
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
  PYTHON_FOR_BUILD RANLIB READELF STRIP LIBZSTD_CFLAGS LIBZSTD_LIBS
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
unset HOSTRUNNER MAKEFLAGS MFLAGS PYTHON_FOR_BUILD PYTHON_FOR_REGEN \
  PYTHONSTRICTEXTENSIONBUILD
if [[ "$legacy_setup_build" -eq 1 ]]; then
  legacy_python_for_build='_PYTHON_PROJECT_BASE=$(abs_builddir)'
  legacy_python_for_build+=' _PYTHON_HOST_PLATFORM=$(_PYTHON_HOST_PLATFORM)'
  legacy_python_for_build+=' PYTHONPATH=$(srcdir)/Lib'
  legacy_python_for_build+=' _PYTHON_SYSCONFIGDATA_NAME=_sysconfigdata_$(ABIFLAGS)_$(MACHDEP)_$(MULTIARCH)'
  legacy_python_for_build+=' _PYTHON_SYSCONFIGDATA_PATH=$(shell test -f pybuilddir.txt && echo $(abs_builddir)/`cat pybuilddir.txt`)'
  legacy_python_for_build+=" $build_python"
  export PYTHON_FOR_BUILD="$legacy_python_for_build"
  export PYTHON_FOR_REGEN="$build_python"
  export PYTHONSTRICTEXTENSIONBUILD=1
fi
export PYTHONDONTWRITEBYTECODE=1

zstd_enabled=0
if [[ "$minor" == 3.14 ]]; then
  zstd_enabled=1
  zstd_archive=$zstd_directory/lib/libzstd.a
  zstd_manifest=$zstd_directory/build-manifest.json
  for path in "$zstd_archive" "$zstd_manifest" \
      "$zstd_directory/include/zstd.h" "$zstd_directory/include/zdict.h"; do
    [[ -f "$path" && ! -L "$path" ]] || {
      echo "error: CPython 3.14 private target zstd input is incomplete: $path" >&2
      exit 1
    }
  done
  [[ ! -e "$zstd_directory/.crossforge-empty" ]] || {
    echo "error: CPython 3.14 received the empty target zstd context" >&2
    exit 1
  }
  export LIBZSTD_CFLAGS="-I$zstd_directory/include"
  export LIBZSTD_LIBS="$zstd_archive -pthread -Wl,--exclude-libs,libzstd.a"
else
  [[ -f "$zstd_directory/.crossforge-empty" ]] || {
    echo "error: pre-3.14 CPython lacks the controlled empty target zstd context" >&2
    exit 1
  }
  if find "$zstd_directory" -mindepth 1 ! -name .crossforge-empty -print -quit \
      | grep -q .; then
    echo "error: pre-3.14 CPython rejects private target zstd inputs" >&2
    exit 1
  fi
fi

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

configure_arguments=(
  --build="$build"
  --host="$target"
  --prefix="$prefix"
)
if [[ "$legacy_setup_build" -eq 0 ]]; then
  configure_arguments+=(
    --with-build-python="$build_python"
    --with-pkg-config=yes
  )
fi
configure_arguments+=(
  --with-computed-gotos=yes
  --with-ensurepip=no
  --disable-test-modules
)
"$source_directory/configure" "${configure_arguments[@]}"
if grep -Fq 'unrecognized options:' config.log; then
  echo "error: CPython configure accepted an unsupported option" >&2
  exit 1
fi

if [[ "$zstd_enabled" -eq 1 ]]; then
  expected_zstd_cflags="-I$zstd_directory/include"
  expected_zstd_libs="$zstd_directory/lib/libzstd.a -pthread -Wl,--exclude-libs,libzstd.a"
  grep -Fqx "MODULE__ZSTD_STATE=yes" Makefile \
    && grep -Fqx "MODULE__ZSTD_CFLAGS=$expected_zstd_cflags" Makefile \
    && grep -Fqx "MODULE__ZSTD_LDFLAGS=$expected_zstd_libs" Makefile || {
      echo "error: target CPython Makefile did not bind the private static zstd" >&2
      exit 1
    }
  if grep -E '^MODULE__ZSTD_LDFLAGS=.*(^|[[:space:]])-lzstd([[:space:]]|$)' Makefile; then
    echo "error: target CPython Makefile fell back to dynamic -lzstd" >&2
    exit 1
  fi
fi

mapfile -t python_for_build_lines < <(grep -E '^PYTHON_FOR_BUILD=' Makefile)
[[ ${#python_for_build_lines[@]} -eq 1 ]] || {
  echo "error: Makefile must define PYTHON_FOR_BUILD exactly once" >&2
  exit 1
}
python_for_build_line=${python_for_build_lines[0]}
for required in \
    '_PYTHON_PROJECT_BASE=$(abs_builddir)' \
    '_PYTHON_HOST_PLATFORM=$(_PYTHON_HOST_PLATFORM)' \
    ' PYTHONPATH=$(srcdir)/Lib ' \
    '_PYTHON_SYSCONFIGDATA_NAME=_sysconfigdata_$(ABIFLAGS)_$(MACHDEP)_$(MULTIARCH)' \
    '_PYTHON_SYSCONFIGDATA_PATH=$(shell test -f pybuilddir.txt && echo $(abs_builddir)/`cat pybuilddir.txt`)'; do
  grep -Fq "$required" <<<"$python_for_build_line" || {
    echo "error: build Python isolation token is missing: $required" >&2
    exit 1
  }
done
if grep -Fq 'PYTHONPATH=$(shell' <<<"$python_for_build_line"; then
  echo "error: build Python is not isolated from target extension modules" >&2
  exit 1
fi
grep -Fqx "PYTHON_FOR_REGEN?=$build_python" Makefile || {
  echo "error: Makefile does not bind the exact regeneration Python" >&2
  exit 1
}

if [[ "$legacy_setup_build" -eq 1 ]]; then
  [[ "$python_for_build_line" == "PYTHON_FOR_BUILD=$legacy_python_for_build" ]] || {
    echo "error: legacy Makefile changed the exact decorated build Python" >&2
    exit 1
  }
  if grep -Eq '^HOSTRUNNER=' Makefile; then
    echo "error: legacy CPython unexpectedly defines HOSTRUNNER" >&2
    exit 1
  fi
  grep -Eq '^sharedmods:[[:space:]].*pybuilddir\.txt' Makefile \
    && grep -Fq '$(PYTHON_FOR_BUILD) $(srcdir)/setup.py $$quiet build' Makefile \
    && grep -Fq '$(PYTHON_FOR_BUILD) $(srcdir)/setup.py install' Makefile || {
      echo "error: legacy cross CPython lacks its setup.py extension paths" >&2
      exit 1
    }
else
  grep -Fq "$build_python" <<<"$python_for_build_line" || {
    echo "error: Makefile does not use the matching build Python" >&2
    exit 1
  }
  grep -Eq '^HOSTRUNNER=[[:space:]]*$' Makefile || {
    echo "error: target execution leaked into the cross-build stage" >&2
    exit 1
  }
fi

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

if [[ "$zstd_enabled" -eq 1 ]]; then
  mapfile -t zstd_modules < <(
    find "$prefix/lib/python$minor/lib-dynload" -maxdepth 1 \
      -name '_zstd.*.so' -type f -print
  )
  [[ ${#zstd_modules[@]} -eq 1 ]] || {
    echo "error: target CPython 3.14 must install exactly one _zstd module" >&2
    exit 1
  }
  zstd_dynamic=$($READELF --wide -d "${zstd_modules[0]}")
  if grep -E '(TEXTREL|RPATH|RUNPATH|NEEDED.*libzstd)' <<<"$zstd_dynamic"; then
    echo "error: target CPython _zstd has a forbidden dynamic property" >&2
    exit 1
  fi
  zstd_symbols=$($READELF --wide --dyn-syms "${zstd_modules[0]}")
  if grep -E '[[:space:]](ZSTD_|ZDICT_|FSE_|HUF_|XXH_)' <<<"$zstd_symbols"; then
    echo "error: target CPython _zstd exposes private zstd symbols" >&2
    exit 1
  fi
  mkdir -p "$prefix/.crossforge"
  install -m 0644 "$zstd_manifest" "$prefix/.crossforge/zstd-build.json"
else
  [[ -z "$(find "$prefix" \( -name '_zstd.*.so' -o \
      -path '*/.crossforge/zstd-build.json' \) -print -quit)" ]] || {
    echo "error: pre-3.14 target CPython unexpectedly contains private zstd output" >&2
    exit 1
  }
fi

headers=$($READELF -h "$python")
grep -F "Machine:" <<<"$headers" | grep -F "$target_machine" >/dev/null || {
  echo "error: target Python has the wrong ELF machine" >&2
  exit 1
}

echo "built cross CPython $version for $target at $prefix"
