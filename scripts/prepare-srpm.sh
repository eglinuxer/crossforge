#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "usage: $0 SRPM SRPM_SHA256 REPOSITORY_NEVRA HEADER_ARCH SPEC SPEC_SHA256 SOURCE_DIR OUTPUT KEY KEY_SHA256 KEY_FINGERPRINT PREP_TARGET_ARCH" >&2
  exit 2
}

[[ $# -eq 12 ]] || usage

srpm=$1
srpm_sha256=$2
expected_nevra=$3
expected_header_arch=$4
spec_name=$5
spec_sha256=$6
source_directory=$7
output=$8
key=$9
key_sha256=${10}
key_fingerprint=${11}
prep_target_arch=${12}

[[ -f "$srpm" && -f "$key" ]] || usage
[[ "$srpm_sha256" =~ ^[0-9a-f]{64}$ ]] || usage
[[ "$spec_sha256" =~ ^[0-9a-f]{64}$ ]] || usage
[[ "$key_sha256" =~ ^[0-9a-f]{64}$ ]] || usage
[[ "$key_fingerprint" =~ ^[0-9a-f]{40}$ ]] || usage
[[ "$prep_target_arch" == x86_64 || "$prep_target_arch" == aarch64 ]] || usage
[[ "$spec_name" != */* && "$source_directory" != */* ]] || usage
[[ ! -e "$output" ]] || {
  echo "error: output already exists: $output" >&2
  exit 1
}

actual_sha256=$(sha256sum "$srpm")
actual_sha256=${actual_sha256%% *}
[[ "$actual_sha256" == "$srpm_sha256" ]] || {
  echo "error: SRPM SHA256 mismatch: $srpm" >&2
  exit 1
}

rhel_macro=$(rpm --eval '%{?rhel}')
target_cpu=$(rpm --eval '%{_target_cpu}')
[[ "$rhel_macro" == 8 && "$target_cpu" == x86_64 ]] || {
  echo "error: SRPM prep requires Rocky/RHEL 8 on x86_64" >&2
  exit 1
}

work=$(mktemp -d /tmp/crossforge-srpm.XXXXXX)
trap 'rm -rf -- "$work"' EXIT
signature_database=$work/signature-rpmdb
mkdir -p "$signature_database"
rpm --dbpath "$signature_database" --initdb
actual_key_sha256=$(sha256sum "$key")
actual_key_sha256=${actual_key_sha256%% *}
[[ "$actual_key_sha256" == "$key_sha256" ]] || {
  echo "error: RPM key SHA256 differs from release.json" >&2
  exit 1
}
rpm --dbpath "$signature_database" --import "$key"
signature_output=$(rpmkeys --dbpath "$signature_database" \
  --checksig --verbose "$srpm")
signature_output=${signature_output,,}
short_key_fingerprint=${key_fingerprint: -8}
[[ "$signature_output" == *"$key_fingerprint"* || \
  "$signature_output" == *"$short_key_fingerprint"* ]] || {
  echo "error: SRPM signature does not use the locked Rocky key" >&2
  exit 1
}
grep -qi 'signature.*: ok' <<<"$signature_output" || {
  echo "error: SRPM signature verification did not report OK" >&2
  exit 1
}
header=$(rpm --dbpath "$signature_database" -qp \
  --qf '%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SOURCEPACKAGE}\n' \
  "$srpm")
IFS=$'\t' read -r name epoch version release header_arch source_package <<<"$header"
actual_nevra=${name}-${epoch}:${version}-${release}.src
[[ "$source_package" == 1 && "$actual_nevra" == "$expected_nevra" && \
  "$header_arch" == "$expected_header_arch" ]] || {
  echo "error: SRPM header identity differs from release.json" >&2
  exit 1
}

topdir=$work/rpmbuild
mkdir -p "$topdir/BUILD" "$topdir/RPMS" "$topdir/SOURCES" \
  "$topdir/SPECS" "$topdir/SRPMS"

rpm -ivh --nodeps --define "_topdir $topdir" "$srpm" >/dev/null
spec=$topdir/SPECS/$spec_name
[[ -f "$spec" ]] || {
  echo "error: SRPM did not contain $spec_name" >&2
  exit 1
}
actual_spec_sha256=$(sha256sum "$spec")
actual_spec_sha256=${actual_spec_sha256%% *}
[[ "$actual_spec_sha256" == "$spec_sha256" ]] || {
  echo "error: extracted spec SHA256 mismatch: $spec_name" >&2
  exit 1
}

# BuildRequires covers the SRPM's native build, documentation and tests, while
# Crossforge only delegates source unpacking and vendor patch application to
# %prep. The prep tool closure must be locked separately before a candidate.
LC_ALL=C rpmbuild -bp --nodeps --target "$prep_target_arch" \
  --define "_topdir $topdir" "$spec"
prepared=$topdir/BUILD/$source_directory
[[ -d "$prepared" ]] || {
  echo "error: rpmbuild did not produce $source_directory" >&2
  exit 1
}

mkdir -p "$(dirname "$output")"
cp -a "$prepared" "$output"
mkdir -p "$output/.crossforge"
LC_ALL=C rpmspec -P --target "$prep_target_arch" \
  --define "_topdir $topdir" "$spec" \
  >"$output/.crossforge/spec.expanded"
cp "$spec" "$output/.crossforge/spec"
{
  printf 'srpm_sha256=%s\n' "$srpm_sha256"
  printf 'srpm_nevra=%s\n' "$expected_nevra"
  printf 'srpm_header_arch=%s\n' "$expected_header_arch"
  printf 'spec_sha256=%s\n' "$spec_sha256"
  printf 'rpm_key_sha256=%s\n' "$key_sha256"
  printf 'rpm_key_fingerprint=%s\n' "$key_fingerprint"
  printf 'rhel_macro=%s\n' "$rhel_macro"
  printf 'dist_macro=%s\n' "$(rpm --eval '%{?dist}')"
  printf 'target_cpu=%s\n' "$target_cpu"
  printf 'prep_target_arch=%s\n' "$prep_target_arch"
} >"$output/.crossforge/preparation.txt"

echo "prepared: $source_directory -> $output"
