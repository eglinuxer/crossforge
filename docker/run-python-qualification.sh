#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 5 && $# -ne 6 ]]; then
  echo "usage: $0 ROW VERSION ADAPTER ARCH TARGET [QEMU]" >&2
  exit 2
fi

row=$1
version=$2
adapter=$3
arch=$4
target=$5
qemu=${6:-}
minor=${version%.*}

case "$arch:$target" in
  x86_64:x86_64-unknown-linux-gnu)
    [[ -z "$qemu" ]] || {
      echo "error: x86_64 qualification must not receive QEMU" >&2
      exit 1
    }
    ;;
  aarch64:aarch64-unknown-linux-gnu)
    [[ -n "$qemu" && -f "$qemu" ]] || {
      echo "error: aarch64 qualification requires explicit QEMU" >&2
      exit 1
    }
    ;;
  *)
    echo "error: architecture/target mismatch: $arch/$target" >&2
    exit 1
    ;;
esac

release=/src/config/release.json
work=/work/qualification/python/$row/$arch
compile_report=$work/compile.json
target_prefix=/opt/crossforge/python/$row/targets/$target
runtime_evidence=/work/qualification/python/runtime-clean-$arch.json
probe=/work/tests/python/runtime_probe.py

/usr/libexec/platform-python /work/scripts/verify-python-row.py \
  --release "$release" \
  --row "$row" \
  --version "$version" \
  --adapter "$adapter"

extension_name=$(
  /usr/libexec/platform-python -c '
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
name = value.get("extension", {}).get("name")
if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
    raise SystemExit("invalid extension name in compile report")
print(name)
' "$compile_report"
)
extension=$work/$extension_name

common=(
  --compile-report "$compile_report"
  --release "$release"
  --target-prefix "$target_prefix"
  --extension "$extension"
  --probe "$probe"
  --target "$target"
  --version "$version"
)
qemu_arguments=()
if [[ -n "$qemu" ]]; then
  qemu_arguments=(--qemu "$qemu")
fi

/usr/libexec/platform-python /work/scripts/run-cpython-runtime.py \
  "${common[@]}" \
  --runtime-root /runtime-locked \
  --tier locked-sysroot \
  "${qemu_arguments[@]}" \
  --output "$work/locked-sysroot.json"

/usr/libexec/platform-python /work/scripts/run-cpython-runtime.py \
  "${common[@]}" \
  --runtime-root /runtime-clean \
  --runtime-evidence "$runtime_evidence" \
  --tier clean-rocky \
  "${qemu_arguments[@]}" \
  --output "$work/clean-rocky.json"

/usr/libexec/platform-python /work/scripts/finalize-cpython-qualification.py \
  --compile-report "$compile_report" \
  --locked-sysroot-result "$work/locked-sysroot.json" \
  --clean-runtime-result "$work/clean-rocky.json" \
  --release "$release" \
  --target "$target" \
  --version "$version" \
  --output "/work/qualification/python/$row/$arch.json"
