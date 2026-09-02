#!/usr/bin/env python3
"""Pure validation for content-locked zstd build and compile evidence."""

import hashlib
import json
import re


class ZstdEvidenceError(RuntimeError):
    pass


FAMILY = re.compile(r"(?:ZSTD|ZDICT|FSE|HUF|XXH)_")
REQUIRED_DEFINITIONS = [
    "ZSTD_compressStream2",
    "ZSTD_decompressStream",
    "ZSTD_versionNumber",
]
MANIFEST_KEYS = {
    "schema_version",
    "kind",
    "version",
    "identity",
    "prefix",
    "compiler_dumpmachine",
    "flags",
    "archive",
    "headers",
    "pic_probe",
    "source_manifest_sha256",
    "build_policy",
    "build_component",
    "policy",
}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def require(condition, message):
    if not condition:
        raise ZstdEvidenceError(message)


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def serialized_sha256(value):
    payload = (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def require_exact_keys(value, expected, path):
    require(isinstance(value, dict), "%s must be an object" % path)
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    require(not missing, "%s is missing field(s): %s" % (path, ", ".join(missing)))
    require(not unknown, "%s has unknown field(s): %s" % (path, ", ".join(unknown)))


def require_string(value, path):
    require(isinstance(value, str) and value, "%s must be a non-empty string" % path)
    return value


def require_sha256(value, path):
    require_string(value, path)
    require(SHA256.fullmatch(value) is not None, "%s must be a lowercase SHA256" % path)
    return value


def expected_components(release, target_arch, render_component_documents):
    try:
        zstd = release["python"]["zstd"]
        require(
            zstd["version"] == "1.5.7"
            and zstd["source"]["status"] == "locked",
            "release zstd source/version is not locked",
        )
        documents = render_component_documents(release)
        names = {
            "policy": "implementation/zstd-build-policy",
            "host": "zstd/host-build",
            "target": "zstd/%s-build" % target_arch,
        }
        return {
            role: {
                "component": component,
                "canonical_sha256": canonical_sha256(documents[component]),
            }
            for role, component in names.items()
        }
    except ZstdEvidenceError:
        raise
    except (KeyError, TypeError, RuntimeError) as error:
        raise ZstdEvidenceError(
            "release zstd component graph is invalid: %s" % error
        ) from error


def validate_no_dynamic_libzstd(elf_audit, path):
    """Reject a dynamic zstd dependency anywhere in a complete ELF audit."""
    require(isinstance(elf_audit, dict), "%s must be an object" % path)
    for name, audit in elf_audit.items():
        require(isinstance(name, str) and name, "%s has an invalid ELF path" % path)
        require(isinstance(audit, dict), "%s %s must be an object" % (path, name))
        needed = audit.get("needed")
        require(
            isinstance(needed, list),
            "%s %s needed must be an array" % (path, name),
        )
        require(
            not any(
                isinstance(dependency, str)
                and dependency.startswith("libzstd.so")
                for dependency in needed
            ),
            "%s %s dynamically depends on libzstd" % (path, name),
        )
    return elf_audit


def validate_build_manifest(
    document,
    identity,
    prefix,
    machine,
    component_identity,
    policy_identity,
    path,
):
    require_exact_keys(document, MANIFEST_KEYS, path)
    require(
        type(document["schema_version"]) is int
        and document["schema_version"] == 1,
        "%s schema version mismatch" % path,
    )
    require(
        document["kind"] == "crossforge-zstd-static-build"
        and document["version"] == "1.5.7"
        and document["identity"] == identity
        and document["prefix"] == prefix,
        "%s identity mismatch" % path,
    )
    expected_compiler = "x86_64-redhat-linux" if identity == "host" else identity
    require(
        document["compiler_dumpmachine"] == expected_compiler,
        "%s compiler identity mismatch" % path,
    )

    flags = document["flags"]
    require_exact_keys(
        flags, {"cflags", "cppflags", "pic_probe_ldflags"}, path + " flags"
    )
    for name in flags:
        require_string(flags[name], "%s flags %s" % (path, name))
    cflags = flags["cflags"].split()
    require(
        len(cflags) == 5
        and cflags[:4] == ["-O2", "-g0", "-fPIC", "-fvisibility=hidden"]
        and re.fullmatch(
            r"-ffile-prefix-map=/[^= ]+=/usr/src/debug/crossforge-zstd",
            cflags[4],
        ),
        "%s CFLAGS differ from the locked PIC/visibility policy" % path,
    )
    require(
        flags["cppflags"]
        == "-DZSTD_MULTITHREAD -DZSTD_NO_TRACE -DDEBUGLEVEL=0 "
        "-DZSTDLIB_VISIBLE=ZSTDLIB_HIDDEN "
        "-DZSTDERRORLIB_VISIBLE=ZSTDERRORLIB_HIDDEN "
        "-DZDICTLIB_VISIBLE=ZDICTLIB_HIDDEN "
        "-DZSTDLIB_STATIC_API=ZSTDLIB_HIDDEN "
        "-DZDICTLIB_STATIC_API=ZDICTLIB_HIDDEN"
        and flags["pic_probe_ldflags"]
        == "-shared -Wl,-z,defs,-z,text -Wl,--whole-archive lib/libzstd.a "
        "-Wl,--no-whole-archive,--exclude-libs,libzstd.a -pthread",
        "%s locked flags mismatch" % path,
    )

    archive = document["archive"]
    require_exact_keys(
        archive, {"path", "sha256", "members", "objects"}, path + " archive"
    )
    require(archive["path"] == "lib/libzstd.a", "%s archive path mismatch" % path)
    require_sha256(archive["sha256"], path + " archive sha256")
    members = archive["members"]
    require(isinstance(members, list) and members, "%s archive members are empty" % path)
    names = []
    for index, member in enumerate(members):
        member_path = "%s archive member %d" % (path, index)
        require_exact_keys(member, {"name", "sha256"}, member_path)
        name = require_string(member["name"], member_path + " name")
        require(
            "/" not in name and name not in (".", "..") and name.endswith(".o"),
            "%s has an unsafe name" % member_path,
        )
        require_sha256(member["sha256"], member_path + " sha256")
        names.append(name)
    require(names == sorted(set(names)), "%s archive members are not canonical" % path)
    require(
        type(archive["objects"]) is int and archive["objects"] == len(members),
        "%s archive object count mismatch" % path,
    )

    headers = document["headers"]
    require_exact_keys(
        headers, {"zstd.h", "zstd_errors.h", "zdict.h"}, path + " headers"
    )
    for name in headers:
        require_sha256(headers[name], "%s header %s" % (path, name))
    probe = document["pic_probe"]
    require_exact_keys(
        probe,
        {
            "sha256",
            "machine",
            "whole_archive",
            "no_zstd_exports",
            "no_dynamic_libzstd",
            "no_rpath",
        },
        path + " pic_probe",
    )
    require_sha256(probe["sha256"], path + " pic_probe sha256")
    require(
        probe["machine"] == machine
        and probe["whole_archive"] is True
        and probe["no_zstd_exports"] is True
        and probe["no_dynamic_libzstd"] is True
        and probe["no_rpath"] is True,
        "%s PIC probe evidence mismatch" % path,
    )
    require_sha256(document["source_manifest_sha256"], path + " source manifest sha256")
    for name, expected in (
        ("build_policy", policy_identity),
        ("build_component", component_identity),
    ):
        reference = document[name]
        require_exact_keys(
            reference,
            {"component", "canonical_sha256"},
            "%s %s" % (path, name),
        )
        require_sha256(reference["canonical_sha256"], "%s %s digest" % (path, name))
        require(reference == expected, "%s %s identity mismatch" % (path, name))
    require_exact_keys(
        document["policy"],
        {
            "static_only",
            "position_independent",
            "multithread",
            "no_trace",
            "debug_level",
            "visibility",
            "legacy_support",
            "exclude_archive_symbols",
        },
        path + " policy",
    )
    require(
        document["policy"]
        == {
            "static_only": True,
            "position_independent": True,
            "multithread": True,
            "no_trace": True,
            "debug_level": 0,
            "visibility": "hidden",
            "legacy_support": 0,
            "exclude_archive_symbols": True,
        },
        "%s build policy mismatch" % path,
    )
    return document


def validate_build_evidence(
    value,
    identity,
    prefix,
    machine,
    component_identity,
    policy_identity,
    path,
):
    require_exact_keys(value, {"manifest", "manifest_sha256"}, path)
    require_sha256(value["manifest_sha256"], path + " manifest_sha256")
    validate_build_manifest(
        value["manifest"],
        identity,
        prefix,
        machine,
        component_identity,
        policy_identity,
        path + " manifest",
    )
    require(
        value["manifest_sha256"] == serialized_sha256(value["manifest"]),
        "%s serialized manifest digest mismatch" % path,
    )
    return value
