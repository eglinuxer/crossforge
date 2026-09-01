#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 6 ]]; then
  echo "usage: $0 RUNTIME_ROOT QEMU_GUEST_PATH CPU UNAME_RELEASE TIER RESULT" >&2
  exit 2
fi

runtime_root=$1
qemu_guest=$2
cpu=$3
uname_release=$4
tier=$5
result_file=$6
artifacts=/opt/crossforge-qualification

[[ "$runtime_root" == /* && "$runtime_root" != / ]] || {
  echo "error: unsafe runtime root: $runtime_root" >&2
  exit 1
}
[[ "$qemu_guest" == /.crossforge/qemu-aarch64 ]] || {
  echo "error: unexpected QEMU guest path: $qemu_guest" >&2
  exit 1
}
[[ "$cpu" == cortex-a53 && "$uname_release" == 4.18.0 ]] || {
  echo "error: unqualified QEMU CPU or uname override" >&2
  exit 1
}
[[ "$tier" == locked-sysroot || "$tier" == clean-rocky ]] || {
  echo "error: unsupported runtime tier: $tier" >&2
  exit 1
}
[[ "$result_file" == /work/qualification/*.result ]] || {
  echo "error: unsafe runtime result path: $result_file" >&2
  exit 1
}
[[ -f /work/scripts/loader_evidence.py ]] || {
  echo "error: deterministic loader normalizer is missing" >&2
  exit 1
}
[[ -x "$runtime_root$qemu_guest" ]] || {
  echo "error: QEMU executor is not mounted in the runtime root" >&2
  exit 1
}
if [[ "$tier" == locked-sysroot ]]; then
  [[ -f "$runtime_root/usr/share/crossforge/sysroot-lock.json" ]] || {
    echo "error: locked-sysroot tier is missing its embedded lock" >&2
    exit 1
  }
else
  [[ ! -e "$runtime_root/usr/share/crossforge/sysroot-lock.json" ]] || {
    echo "error: clean-rocky tier unexpectedly contains a Crossforge sysroot lock" >&2
    exit 1
  }
fi
grep -Eq '^ID="?rocky"?$' "$runtime_root/etc/os-release" || {
  echo "error: runtime root is not Rocky Linux" >&2
  exit 1
}
grep -Eq '^VERSION_ID="?8\.10"?$' "$runtime_root/etc/os-release" || {
  echo "error: runtime root is not Rocky Linux 8.10" >&2
  exit 1
}
file -L "$runtime_root/lib/ld-linux-aarch64.so.1" | grep -F 'ARM aarch64' >/dev/null || {
  echo "error: runtime root does not contain the aarch64 loader" >&2
  exit 1
}

for artifact in hello modern lto lto-archive libgcc-helper libthrow.so catch; do
  [[ -f "$runtime_root$artifacts/$artifact" ]] || {
    echo "error: missing qualification artifact: $artifact" >&2
    exit 1
  }
done

run_qemu() {
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    timeout 30s chroot "$runtime_root" \
      "$qemu_guest" -L / -cpu "$cpu" -r "$uname_release" "$@"
}

loader_listing=$(run_qemu \
  -E LD_TRACE_LOADED_OBJECTS=1 "$artifacts/catch" 2>&1)
[[ -n "$loader_listing" ]] || {
  echo "error: $tier dynamic-loader trace was empty" >&2
  exit 1
}
[[ "$loader_listing" != *'not found'* ]] || {
  echo "error: $tier loader could not resolve target dependencies" >&2
  printf '%s\n' "$loader_listing" >&2
  exit 1
}
loader_evidence=$(printf '%s\n' "$loader_listing" \
  | /usr/libexec/platform-python /work/scripts/loader_evidence.py)

hello_output=$(run_qemu "$artifacts/hello")
[[ "$hello_output" == crossforge-c-ok ]] || {
  echo "error: aarch64 C smoke output mismatch" >&2
  exit 1
}
modern_output=$(run_qemu "$artifacts/modern")
[[ "$modern_output" == crossforge-cxx-ok ]] || {
  echo "error: aarch64 C++ smoke output mismatch" >&2
  exit 1
}

for executable in lto lto-archive libgcc-helper catch; do
  run_qemu "$artifacts/$executable"
done

qemu_version_line=$("$runtime_root$qemu_guest" --version | head -n 1)
qemu_version=${qemu_version_line#qemu-aarch64 version }
qemu_version=${qemu_version%% *}
[[ "$qemu_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "error: could not parse QEMU version: $qemu_version_line" >&2
  exit 1
}

loader_file=$result_file.loader
result_tmp=$result_file.tmp
loader_tmp=$loader_file.tmp
trap 'rm -f "$result_tmp" "$loader_tmp"' EXIT
printf '%s\n' "$loader_evidence" >"$loader_tmp"
{
  printf 'schema_version=1\n'
  printf 'tier=%s\n' "$tier"
  printf 'status=passed\n'
  printf 'target=aarch64-unknown-linux-gnu\n'
  printf 'cpu=%s\n' "$cpu"
  printf 'uname_release=%s\n' "$uname_release"
  printf 'qemu_binary_sha256=%s\n' \
    "$(sha256sum "$runtime_root$qemu_guest" | awk '{print $1}')"
  printf 'qemu_version=%s\n' "$qemu_version"
  printf 'runtime_os_release_sha256=%s\n' \
    "$(sha256sum "$runtime_root/etc/os-release" | awk '{print $1}')"
  printf 'loader_sha256=%s\n' \
    "$(sha256sum "$runtime_root/lib/ld-linux-aarch64.so.1" | awk '{print $1}')"
  printf 'loader_evidence_sha256=%s\n' \
    "$(sha256sum "$loader_tmp" | awk '{print $1}')"
  printf 'hello_stdout_sha256=%s\n' \
    "$(printf '%s' "$hello_output" | sha256sum | awk '{print $1}')"
  printf 'modern_stdout_sha256=%s\n' \
    "$(printf '%s' "$modern_output" | sha256sum | awk '{print $1}')"
} >"$result_tmp"
mv "$loader_tmp" "$loader_file"
mv "$result_tmp" "$result_file"
trap - EXIT

echo "qemu-qualified: aarch64-unknown-linux-gnu ($tier, $cpu, uname $uname_release)"
