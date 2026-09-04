#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 7 ]]; then
  echo "usage: $0 GCC_BUILD DESTDIR PREFIX SYSROOT TARGET GCC_MAJOR ARCH" >&2
  exit 2
fi

gcc_build=$1
destdir=$2
prefix=$3
sysroot=$4
target=$5
gcc_major=$6
arch=$7

[[ "$prefix" == /opt/crossforge/targets/"$target" ]] || {
  echo "error: prefix and target disagree" >&2
  exit 1
}
[[ "$destdir" == /* && "$destdir" != / && "$destdir" != *'/../'* ]] || {
  echo "error: unsafe DESTDIR: $destdir" >&2
  exit 1
}
[[ "$gcc_build" == /* && "$gcc_build" != / && "$gcc_major" =~ ^[0-9]+$ ]] || {
  echo "error: unsafe GCC build path or major version" >&2
  exit 1
}

case "$arch:$target" in
  x86_64:x86_64-unknown-linux-gnu)
    output_format=elf64-x86-64
    ;;
  aarch64:aarch64-unknown-linux-gnu)
    output_format=elf64-littleaarch64
    ;;
  *)
    echo "error: arch and target disagree" >&2
    exit 1
    ;;
esac
[[ "$sysroot" == /opt/crossforge/sysroots/el8/"$arch" ]] || {
  echo "error: unexpected sysroot path: $sysroot" >&2
  exit 1
}

gcc_lib=$destdir$prefix/lib/gcc/$target/$gcc_major
nonshared=$gcc_build/$target/libstdc++-v3/src/.libs/libstdc++_nonshared80.a
libgcc_eh=$gcc_lib/libgcc_eh.a
[[ -d "$gcc_lib" && -s "$nonshared" && -s "$libgcc_eh" ]] || {
  echo "error: incomplete GCC install or missing vendor nonshared archive" >&2
  exit 1
}
for symbol in __gcc_nested_func_ptr_created __gcc_nested_func_ptr_deleted; do
  nm -g --defined-only "$libgcc_eh" \
    | grep -E "[[:space:]][TW] $symbol$" >/dev/null || {
    echo "error: cross-built libgcc_eh.a lacks $symbol" >&2
    exit 1
  }
done
[[ -e "$sysroot/usr/lib64/libstdc++.so.6" ]] || {
  echo "error: EL8 libstdc++ runtime is missing" >&2
  exit 1
}
[[ -e "$sysroot/lib64/libgcc_s.so.1" ]] || {
  echo "error: EL8 libgcc runtime is missing" >&2
  exit 1
}

while IFS= read -r -d '' library; do
  rm -f -- "$library"
done < <(
  find "$destdir$prefix" \( -type f -o -type l \) \
    \( -name 'libstdc++.so*' -o -name 'libgcc_s.so*' \) -print0
)

find "$destdir$prefix" -type f \
  \( -name 'libstdc++.a' -o -name 'libstdc++.la' -o -name 'libgcc_s.a' \) \
  -delete

install -m 0644 "$nonshared" "$gcc_lib/libstdc++_nonshared.a"
[[ -n "$(ar t "$gcc_lib/libstdc++_nonshared.a")" ]] || {
  echo "error: libstdc++_nonshared.a is empty" >&2
  exit 1
}

printf '%s\n' \
  "OUTPUT_FORMAT($output_format)" \
  'INPUT (' \
  '  =/usr/lib64/libstdc++.so.6' \
  '  -lstdc++_nonshared' \
  '  AS_NEEDED ( =/usr/lib64/libstdc++.so.6 )' \
  ')' >"$gcc_lib/libstdc++.so"
printf '%s\n' \
  "OUTPUT_FORMAT($output_format)" \
  'GROUP ( =/lib64/libgcc_s.so.1 libgcc.a libgcc_eh.a )' \
  >"$gcc_lib/libgcc_s.so"

remaining=$(find "$destdir$prefix" \( -type f -o -type l \) \
  \( -name 'libstdc++.so.6*' -o -name 'libgcc_s.so.1' \) -print -quit)
if [[ -n "$remaining" ]]; then
  echo "error: newly built shared runtimes remain in the SDK" >&2
  exit 1
fi
[[ -f "$gcc_lib/libgcc.a" ]] || {
  echo "error: cross-built libgcc.a is missing" >&2
  exit 1
}

echo "installed EL8 hybrid runtime for $target"
