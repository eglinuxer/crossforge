#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 17 ]]; then
  echo "usage: $0 SOURCE BUILD PREFIX SOURCE_MANIFEST SOURCE_ARCHIVE SOURCE_SIGNATURE BUILD_MANIFEST SOURCE_COMPONENT SOURCE_SHA256 POLICY_COMPONENT POLICY_SHA256 BUILD_COMPONENT BUILD_SHA256 HOST_OR_TRIPLE TOOLCHAIN SYSROOT_OR_DASH JOBS" >&2
  exit 2
fi

source_directory=$1
build_directory=$2
prefix=$3
source_manifest=$4
source_archive=$5
source_signature=$6
build_manifest=$7
source_component=$8
source_sha256=$9
policy_component=${10}
policy_sha256=${11}
build_component=${12}
build_sha256=${13}
identity=${14}
toolchain=${15}
sysroot=${16}
jobs=${17}
version=1.5.7

[[ "$source_directory" = /* && "$build_directory" = /* && "$prefix" = /* ]] || {
  echo "error: zstd source/build/prefix paths must be absolute" >&2
  exit 1
}
[[ -f "$source_manifest" && -f "$source_archive" && -f "$source_signature" && -f "$source_component" && -f "$source_directory/lib/Makefile" ]] || {
  echo "error: prepared zstd source or manifest is missing" >&2
  exit 1
}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || {
  echo "error: zstd jobs must be positive" >&2
  exit 1
}
[[ ! -e "$build_directory" && ! -L "$build_directory" ]] || {
  echo "error: refusing stale zstd build directory" >&2
  exit 1
}
[[ ! -e "$prefix" && ! -L "$prefix" ]] || {
  echo "error: refusing stale zstd prefix" >&2
  exit 1
}
[[ ! -e "$build_manifest" && ! -L "$build_manifest" ]] || {
  echo "error: refusing stale zstd build manifest" >&2
  exit 1
}

python=${CROSSFORGE_PLATFORM_PYTHON:-}
if [[ -z "$python" ]]; then
  if [[ -x /usr/libexec/platform-python ]]; then
    python=/usr/libexec/platform-python
  else
    python=$(command -v python3)
  fi
fi
"$python" - "$source_component" "$source_sha256" "$source_directory" \
  "$source_manifest" "$source_archive" "$source_signature" \
  "$(dirname "$0")" <<'PY'
import pathlib, runpy, sys

component, digest, source, manifest, archive, signature, scripts = sys.argv[1:]
validator = runpy.run_path(scripts + "/prepare-zstd-source.py")
try:
    validator["validate_prepared_source"](
        pathlib.Path(component),
        digest,
        pathlib.Path(source),
        pathlib.Path(manifest),
        pathlib.Path(archive),
        pathlib.Path(signature),
        pathlib.Path(scripts).parent,
    )
except (OSError, validator["PreparationError"]) as error:
    sys.stderr.write("error: %s\n" % error)
    raise SystemExit(1)
PY
"$python" - "$policy_component" "$policy_sha256" "$(dirname "$0")" <<'PY'
import runpy, sys
path, digest, scripts = sys.argv[1:]
reader = runpy.run_path(scripts + "/release_component.py")
document = reader["load_component"](
    __import__("pathlib").Path(path),
    "implementation/zstd-build-policy",
    "build",
    digest,
)
if document["dependencies"] != []:
    raise SystemExit("error: zstd build policy unexpectedly has dependencies")
actual = {item["path"]: item["value"] for item in document["materials"]}
expected = {
    "/@implementation/zstd/exclude_archive_symbols": True,
    "/@implementation/zstd/linkage": "static",
    "/@implementation/zstd/multithread": True,
    "/@implementation/zstd/no_trace": True,
    "/@implementation/zstd/position_independent_code": True,
    "/@implementation/zstd/private": True,
    "/@implementation/zstd/selected_license": "BSD-3-Clause",
    "/@implementation/zstd/visibility": "hidden",
}
if actual != expected:
    raise SystemExit("error: zstd build policy materials differ")
PY

case "$identity" in
  host)
    [[ "$sysroot" == - && "$prefix" == */zstd/$version/host ]] || {
      echo "error: invalid host zstd prefix/sysroot" >&2
      exit 1
    }
    [[ "$build_component" == zstd/host-build ]] || exit 1
    tool_prefix=
    expected_machine='Advanced Micro Devices X86-64'
    ;;
  x86_64-unknown-linux-gnu)
    [[ "$sysroot" = /* && "$prefix" == */zstd/$version/$identity ]] || {
      echo "error: invalid x86_64 zstd prefix/sysroot" >&2
      exit 1
    }
    tool_prefix=$identity-
    [[ "$build_component" == zstd/x86_64-build ]] || exit 1
    expected_machine='Advanced Micro Devices X86-64'
    ;;
  aarch64-unknown-linux-gnu)
    [[ "$sysroot" = /* && "$prefix" == */zstd/$version/$identity ]] || {
      echo "error: invalid aarch64 zstd prefix/sysroot" >&2
      exit 1
    }
    tool_prefix=$identity-
    [[ "$build_component" == zstd/aarch64-build ]] || exit 1
    expected_machine=AArch64
    ;;
  *)
    echo "error: unsupported zstd build identity: $identity" >&2
    exit 1
    ;;
esac
[[ "$source_sha256" =~ ^[0-9a-f]{64}$ && "$policy_sha256" =~ ^[0-9a-f]{64}$ && "$build_sha256" =~ ^[0-9a-f]{64}$ ]] || {
  echo "error: invalid zstd component digest" >&2
  exit 1
}

for tool in gcc ar ranlib readelf; do
  [[ -x "$toolchain/$tool_prefix$tool" ]] || {
    echo "error: missing zstd build tool: $tool_prefix$tool" >&2
    exit 1
  }
done
cc=("$toolchain/$tool_prefix"gcc)
if [[ "$identity" != host ]]; then
  cc+=("--sysroot=$sysroot")
  [[ "$("${cc[@]}" -dumpmachine)" == "$identity" ]] || {
    echo "error: zstd cross compiler triple differs" >&2
    exit 1
  }
fi
compiler_machine=$("${cc[@]}" -dumpmachine)
ar="$toolchain/$tool_prefix"ar
ranlib="$toolchain/$tool_prefix"ranlib
readelf="$toolchain/$tool_prefix"readelf

mkdir -p "$build_directory"
cp -a "$source_directory/lib" "$build_directory/lib"
cppflags="-DZSTD_MULTITHREAD -DZSTD_NO_TRACE -DDEBUGLEVEL=0 -DZSTDLIB_VISIBLE=ZSTDLIB_HIDDEN -DZSTDERRORLIB_VISIBLE=ZSTDERRORLIB_HIDDEN -DZDICTLIB_VISIBLE=ZDICTLIB_HIDDEN -DZSTDLIB_STATIC_API=ZSTDLIB_HIDDEN -DZDICTLIB_STATIC_API=ZDICTLIB_HIDDEN"
cflags="-O2 -g0 -fPIC -fvisibility=hidden -ffile-prefix-map=$build_directory=/usr/src/debug/crossforge-zstd"
SOURCE_DATE_EPOCH=0 make -C "$build_directory/lib" -j"$jobs" libzstd.a \
  CC="${cc[*]}" AR="$ar" RANLIB="$ranlib" \
  CFLAGS="$cflags" CPPFLAGS="$cppflags" ZSTD_LEGACY_SUPPORT=0

mkdir -p "$prefix/include" "$prefix/lib/pkgconfig" "$prefix/share/licenses/zstd"
install -m 0644 "$build_directory/lib/zstd.h" "$build_directory/lib/zstd_errors.h" \
  "$build_directory/lib/zdict.h" "$prefix/include/"
install -m 0644 "$build_directory/lib/libzstd.a" "$prefix/lib/libzstd.a"
install -m 0644 "$source_directory/LICENSE" "$source_directory/COPYING" \
  "$prefix/share/licenses/zstd/"
cat >"$prefix/lib/pkgconfig/libzstd.pc" <<EOF
prefix=$prefix
exec_prefix=\${prefix}
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: zstd
Description: Crossforge content-locked static Zstandard
Version: $version
Libs: \${libdir}/libzstd.a -pthread
Cflags: -I\${includedir}
EOF

[[ -f "$prefix/lib/libzstd.a" ]] || exit 1
[[ -z "$(find "$prefix" -type f \( -name '*.so' -o -name '*.so.*' \) -print -quit)" ]] || {
  echo "error: zstd build produced a shared library" >&2
  exit 1
}
objects="$build_directory/objects"
mkdir "$objects"
(cd "$objects" && "$ar" x "$prefix/lib/libzstd.a")
mapfile -t object_files < <(find "$objects" -maxdepth 1 -name '*.o' -type f -print | sort)
[[ ${#object_files[@]} -gt 0 ]] || {
  echo "error: zstd archive has no objects" >&2
  exit 1
}
for object in "${object_files[@]}"; do
  "$readelf" -h "$object" | grep -F "Machine:" | grep -F "$expected_machine" >/dev/null || {
    echo "error: zstd archive object has wrong ELF machine: $object" >&2
    exit 1
  }
done

probe_source="$build_directory/probe.c"
cat >"$probe_source" <<'EOF'
#include <zstd.h>
#if ZSTD_VERSION_NUMBER != 10507
# error unexpected zstd headers
#endif
int crossforge_zstd_version(void) { return (int)ZSTD_versionNumber(); }
int main(void) { return crossforge_zstd_version() == 10507 ? 0 : 1; }
EOF
"${cc[@]}" -I"$prefix/include" -fPIC -shared -Wl,-z,defs,-z,text \
  "$probe_source" -Wl,--whole-archive "$prefix/lib/libzstd.a" \
  -Wl,--no-whole-archive,--exclude-libs,libzstd.a -pthread \
  -o "$build_directory/libzstd-pic-probe.so"
"$readelf" -h "$build_directory/libzstd-pic-probe.so" \
  | grep -F "Machine:" | grep -F "$expected_machine" >/dev/null
probe_dynamic=$("$readelf" --wide -d "$build_directory/libzstd-pic-probe.so")
! grep -E '(TEXTREL|RPATH|RUNPATH)' <<<"$probe_dynamic" >/dev/null
! grep -E 'NEEDED.*libzstd' <<<"$probe_dynamic" >/dev/null
probe_symbols=$("$readelf" --wide --dyn-syms "$build_directory/libzstd-pic-probe.so")
! grep -E '[[:space:]](ZSTD_|ZDICT_|FSE_|HUF_|XXH_)' <<<"$probe_symbols" >/dev/null
if [[ "$identity" == host ]]; then
  "${cc[@]}" -I"$prefix/include" "$probe_source" \
    "$prefix/lib/libzstd.a" -pthread -o "$build_directory/zstd-version-probe"
  "$build_directory/zstd-version-probe"
fi

archive_sha=$(sha256sum "$prefix/lib/libzstd.a" | awk '{print $1}')
manifest_sha=$(sha256sum "$source_manifest" | awk '{print $1}')
probe_sha=$(sha256sum "$build_directory/libzstd-pic-probe.so" | awk '{print $1}')
mkdir -p "$(dirname "$build_manifest")"
"$python" - "$build_manifest" "$identity" "$prefix" "$archive_sha" \
  "$manifest_sha" "$objects" "$probe_sha" "$expected_machine" \
  "$compiler_machine" "$cflags" "$cppflags" "$policy_sha256" \
  "$build_component" "$build_sha256" <<'PY'
import hashlib, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
objects = pathlib.Path(sys.argv[6])
members = []
for item in sorted(objects.glob("*.o")):
    members.append({"name": item.name, "sha256": hashlib.sha256(item.read_bytes()).hexdigest()})
prefix = pathlib.Path(sys.argv[3])
headers = {}
for name in ("zstd.h", "zstd_errors.h", "zdict.h"):
    headers[name] = hashlib.sha256((prefix / "include" / name).read_bytes()).hexdigest()
document = {
    "schema_version": 1,
    "kind": "crossforge-zstd-static-build",
    "version": "1.5.7",
    "identity": sys.argv[2],
    "prefix": sys.argv[3],
    "compiler_dumpmachine": sys.argv[9],
    "flags": {
        "cflags": sys.argv[10],
        "cppflags": sys.argv[11],
        "pic_probe_ldflags": "-shared -Wl,-z,defs,-z,text -Wl,--whole-archive lib/libzstd.a -Wl,--no-whole-archive,--exclude-libs,libzstd.a -pthread",
    },
    "archive": {"path": "lib/libzstd.a", "sha256": sys.argv[4], "members": members, "objects": len(members)},
    "headers": headers,
    "pic_probe": {
        "sha256": sys.argv[7],
        "machine": sys.argv[8],
        "whole_archive": True,
        "no_zstd_exports": True,
        "no_dynamic_libzstd": True,
        "no_rpath": True,
    },
    "source_manifest_sha256": sys.argv[5],
    "build_policy": {"component": "implementation/zstd-build-policy", "canonical_sha256": sys.argv[12]},
    "build_component": {"component": sys.argv[13], "canonical_sha256": sys.argv[14]},
    "policy": {
        "static_only": True,
        "position_independent": True,
        "multithread": True,
        "no_trace": True,
        "debug_level": 0,
        "visibility": "hidden",
        "legacy_support": 0,
        "exclude_archive_symbols": True,
    },
}
temporary = path.with_name("." + path.name + ".tmp")
temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(str(temporary), str(path))
PY

echo "built static zstd $version for $identity at $prefix"
