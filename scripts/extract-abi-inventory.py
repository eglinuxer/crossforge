#!/usr/bin/env python3
"""Extract a maintenance-only ABI inventory candidate from provider DSOs.

This tool never creates or updates the reviewed ``abi/`` tree.  Both outputs
must be named explicitly: a candidate inventory and compact extraction
evidence.  Verbatim readelf output is intentionally transient; reproducibility
is bound to the provider bytes, tool version, commands, and inventory digest.

``--root`` is a Docker trust boundary, not an identity oracle.  A clean root
must be copied from the release-pinned OCI child manifest; a locked sysroot
must be produced by the verified lock materializer.  This tool derives the
expected identity from validated release.json and records it, but cannot prove
how an arbitrary directory was populated.
"""

import argparse
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

import abi_contract


EVIDENCE_KIND = "crossforge-abi-extraction-evidence"


def fail(message):
    raise abi_contract.AbiContractError(message)


def run(arguments):
    process = subprocess.run(
        [str(argument) for argument in arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if process.returncode != 0:
        fail(
            "command failed (%s):\n%s"
            % (" ".join(str(argument) for argument in arguments), process.stdout + process.stderr)
        )
    return process.stdout


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def load_valid_release(path, repository):
    validator = runpy.run_path(str(repository / "scripts/validate-release.py"))
    try:
        release = validator["load_json"](Path(path))
        schema = validator["load_json"](
            repository / "config/schemas/release.schema.json"
        )
        validator["validate_schema_subset"](schema)
        validator["validate"](release, schema, schema, "$")
    except validator["ValidationError"] as error:
        fail("release validation failed: %s" % error)
    return release


def source_identity_from_release(release, source_kind, arch, triple):
    if source_kind == "clean-rocky-oci":
        platform = {"x86_64": "amd64", "aarch64": "arm64"}[arch]
        digest = release["base_image"]["manifests"][platform]
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            fail("release clean-root child manifest digest is invalid")
        identity = digest[len("sha256:") :]
    else:
        targets = [
            target
            for target in release["targets"]
            if target["arch"] == arch and target["triple"] == triple
        ]
        if len(targets) != 1 or targets[0]["sysroot"]["status"] != "locked":
            fail("release has no unique locked sysroot for requested target")
        identity = targets[0]["sysroot"]["canonical_sha256"]
    abi_contract._validate_sha256(identity, "release-derived source identity SHA256")
    return identity


def resolve_root(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        fail("ABI extraction root is not a real directory")
    resolved = root.resolve()
    if resolved == Path("/"):
        fail("ABI extraction root must not be the host root")
    return resolved


def resolve_provider_file(root, relative_path):
    abi_contract._validate_manifest_path(relative_path, "provider manifest path")
    unresolved = root.joinpath(*relative_path.split("/"))
    if not unresolved.is_file():
        fail("fixed ABI provider is missing beneath root: %s" % relative_path)
    resolved = unresolved.resolve()
    if not is_below(resolved, root):
        fail("fixed ABI provider escapes the resolved root: %s" % relative_path)
    return resolved


def readelf_soname(dynamic_section):
    matches = re.findall(r"\(SONAME\).*?\[([^\]]+)\]", dynamic_section)
    if len(matches) != 1:
        fail("provider does not expose exactly one DT_SONAME")
    return matches[0]


def readelf_machine(elf_header):
    match = re.search(r"^\s*Machine:\s*(.*?)\s*$", elf_header, re.MULTILINE)
    if match is None:
        fail("provider ELF header has no machine")
    return match.group(1)


def is_below(path, root):
    path_text = str(path)
    root_text = str(root)
    return path_text == root_text or path_text.startswith(root_text + os.sep)


def validate_destination(path, repository, label):
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve()
    if is_below(resolved, (repository / "abi").resolve()):
        fail("%s must never write into the reviewed abi/ tree" % label)
    if not resolved.parent.is_dir():
        fail("%s parent directory does not exist" % label)
    if path.is_symlink():
        fail("%s must not be a symbolic link" % label)
    return resolved


def write_canonical(path, document):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        fail("refusing stale ABI output temporary: %s" % temporary)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            stream.write("\n")
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def extract(arguments):
    repository = Path(__file__).resolve().parents[1]
    candidate_path = validate_destination(arguments.candidate, repository, "candidate")
    evidence_path = validate_destination(arguments.evidence, repository, "evidence")
    if candidate_path == evidence_path:
        fail("candidate and evidence outputs must be distinct")

    target = {"arch": arguments.arch, "triple": arguments.triple}
    abi_contract._validate_target(target, arguments.arch, arguments.triple)
    manifest_path = repository / "config/abi-providers.json"
    manifest = abi_contract.load_provider_manifest(manifest_path)
    manifest_sha256 = abi_contract.canonical_sha256(manifest)
    manifest_target = abi_contract.provider_manifest_target(
        manifest, arguments.arch, arguments.triple
    )
    release = load_valid_release(arguments.release, repository)
    identity_sha256 = source_identity_from_release(
        release,
        arguments.source_kind,
        arguments.arch,
        arguments.triple,
    )
    source = {
        "kind": arguments.source_kind,
        "identity_sha256": identity_sha256,
        "provider_manifest_sha256": manifest_sha256,
    }
    providers = {}
    command_evidence = []
    provider_evidence = []
    root = resolve_root(arguments.root)
    parsed = []
    for provider in manifest_target["providers"]:
        soname = provider["soname"]
        relative_path = provider["path"]
        logical_path = "/" + relative_path
        source_path = resolve_provider_file(root, relative_path)
        parsed.append((soname, logical_path, source_path))

    version_output = run([arguments.readelf, "--version"])
    version_lines = [line for line in version_output.splitlines() if line.strip()]
    if not version_lines:
        fail("readelf --version returned no identity")
    expected_machine = abi_contract.TARGETS[arguments.arch]["readelf_machine"]
    for soname, logical_path, source_path in parsed:
        received_sha256 = file_sha256(source_path)
        commands = (
            ("elf_header", [arguments.readelf, "--wide", "-h", source_path]),
            ("dynamic_section", [arguments.readelf, "--wide", "-d", source_path]),
            ("dynamic_symbols", [arguments.readelf, "--wide", "--dyn-syms", source_path]),
        )
        outputs = {}
        for operation, command in commands:
            outputs[operation] = run(command)
            command_evidence.append(
                {
                    "provider": soname,
                    "operation": operation,
                    "arguments": [
                        str(arguments.readelf),
                        "--wide",
                        {"elf_header": "-h", "dynamic_section": "-d", "dynamic_symbols": "--dyn-syms"}[operation],
                        logical_path,
                    ],
                }
            )
        if readelf_machine(outputs["elf_header"]) != expected_machine:
            fail("provider ELF machine differs from requested target: %s" % soname)
        if readelf_soname(outputs["dynamic_section"]) != soname:
            fail("provider DT_SONAME differs from requested identity: %s" % soname)
        observed_after = file_sha256(source_path)
        if observed_after != received_sha256:
            fail("provider changed while readelf evidence was collected: %s" % soname)
        provider = abi_contract.provider_inventory_from_readelf(
            logical_path,
            soname,
            received_sha256,
            outputs["dynamic_symbols"],
        )
        providers[soname] = provider
        provider_evidence.append(
            {
                "soname": soname,
                "path": logical_path,
                "sha256": received_sha256,
                "counts": {
                    "public_versioned_exports": len(provider["exports"]),
                    "unversioned_exports": len(provider["unversioned_exports"]),
                    "nonpublic_versioned_exports": len(provider["nonpublic_versioned_exports"]),
                },
            }
        )

    inventory = {
        "$schema": abi_contract.INVENTORY_SCHEMA_ID,
        "schema_version": 1,
        "kind": abi_contract.INVENTORY_KIND,
        "target": target,
        "source": source,
        "providers": providers,
    }
    abi_contract.validate_inventory(inventory, arguments.arch, arguments.triple)
    abi_contract.validate_inventory_provider_manifest(inventory, manifest)
    inventory_sha256 = abi_contract.canonical_sha256(inventory)
    evidence = {
        "schema_version": 1,
        "kind": EVIDENCE_KIND,
        "target": target,
        "source": source,
        "root_trust_boundary": (
            "docker-copy-from-release-pinned-oci-child"
            if arguments.source_kind == "clean-rocky-oci"
            else "verified-materialization-of-release-pinned-sysroot-lock"
        ),
        "provider_manifest": {
            "path": "config/abi-providers.json",
            "canonical_sha256": manifest_sha256,
        },
        "tool": {
            "name": "readelf",
            "command": str(arguments.readelf),
            "version": version_lines[0],
        },
        "commands": command_evidence,
        "providers": provider_evidence,
        "inventory": {
            "canonical_sha256": inventory_sha256,
            "provider_count": len(providers),
        },
    }
    write_canonical(evidence_path, evidence)
    write_canonical(candidate_path, inventory)
    return inventory, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", required=True, choices=sorted(abi_contract.TARGETS))
    parser.add_argument("--triple", required=True)
    parser.add_argument("--source-kind", required=True, choices=sorted(abi_contract.SOURCE_KINDS))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--readelf", default="readelf")
    arguments = parser.parse_args()
    try:
        extract(arguments)
    except abi_contract.AbiContractError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
