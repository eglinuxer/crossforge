"""Bind clean Python runtime roots to their RPM inputs, preserving legacy evidence."""

import hashlib
import json
from pathlib import Path
import re
import runpy


class OverlayError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise OverlayError(message)


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def identity_fields(version):
    require(type(version) is int and version in (1, 2), "unsupported runtime overlay schema")
    return {"base_image", "target", "sysroot", "selected_packages", "selected_packages_sha256",
            "release_sha256" if version == 1 else "input_binding"}


def validate_binding(value, arch):
    require(arch in ("x86_64", "aarch64"), "unsupported runtime overlay architecture")
    require(type(value) is dict and set(value) == {"kind", "component", "scope", "canonical_sha256"},
            "runtime overlay input binding fields differ")
    require(value["kind"] == "release-component" and value["scope"] == "build" and
            value["component"] == "rpm/sysroot-" + arch, "runtime overlay must bind its target sysroot component")
    require(type(value["canonical_sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", value["canonical_sha256"]),
            "invalid runtime overlay component SHA256")
    return dict(value)


def binding_from_release(release, arch, render_components):
    name = "rpm/sysroot-" + arch
    document = render_components(release)[name]
    return validate_binding({"kind": "release-component", "component": name, "scope": "build",
                             "canonical_sha256": canonical_sha256(document)}, arch)


def validate_identity_binding(evidence, release, arch, render_components):
    version = evidence.get("schema_version")
    fields = identity_fields(version)
    identity = evidence.get("identity")
    require(type(identity) is dict and set(identity) == fields, "runtime overlay identity fields differ")
    if version == 1:
        require(identity["release_sha256"] == canonical_sha256(release), "runtime overlay release digest mismatch")
    else:
        actual = validate_binding(identity["input_binding"], arch)
        require(actual == binding_from_release(release, arch, render_components),
                "runtime overlay component differs from current release inputs")


def component_base(path, name, digest, arch):
    """Read the runtime image pin after authenticating the existing RPM projection."""
    validate_binding({"kind": "release-component", "component": name, "scope": "build", "canonical_sha256": digest}, arch)
    reader = runpy.run_path(str(Path(__file__).with_name("release_component.py")))
    try:
        document = reader["load_component"](path, name, "build", digest)
        require(not document["dependencies"], "runtime sysroot projection has unexpected dependencies")
        def leaf(pointer):
            return reader["material_value"](document, name, "build", digest, pointer, "string")
        require(leaf("/baseline") == "el8", "unsupported runtime overlay baseline")
        base = {"index_digest": leaf("/base_image/digest"),
                "manifest_digest": leaf("/base_image/manifests/" + ("amd64" if arch == "x86_64" else "arm64"))}
        require(all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in base.values()),
                "invalid runtime image digest in component")
        return base
    except reader["ComponentError"] as error:
        raise OverlayError(str(error)) from error
