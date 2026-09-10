"""Verify local OCI bytes; leave filesystem layer application to BuildKit.

An intact layout proves content identity, not qualification or producer trust.
The expected root digest must come from an independently trusted receipt.
"""

import hashlib
import json
from pathlib import Path
import re

from .identity import (digest_value, file_record, parse_json,
                       relative_path, require)


INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYERS = {"application/vnd.oci.image.layer.v1.tar",
          "application/vnd.oci.image.layer.v1.tar+gzip",
          "application/vnd.oci.image.layer.v1.tar+zstd"}
METADATA_LIMIT = 16 * 1024 * 1024


def _metadata_bytes(layout, path, expected_digest=None):
    record = file_record(layout, path)
    if expected_digest is not None:
        require("sha256:" + record["sha256"] == expected_digest, "OCI metadata digest differs")
    with (Path(layout) / path).open("rb") as stream:
        data = stream.read(METADATA_LIMIT + 1)
    require(len(data) <= METADATA_LIMIT, "OCI metadata exceeds size limit")
    require(hashlib.sha256(data).hexdigest() == record["sha256"],
            "OCI metadata changed while reading")
    return data


def descriptor(value, media_types=None):
    require(type(value) is dict, "OCI descriptor must be an object")
    digest_value(value.get("digest"), "OCI descriptor digest", oci=True)
    require(type(value.get("size")) is int and value["size"] >= 0,
            "OCI descriptor size is invalid")
    require(type(value.get("mediaType")) is str, "OCI descriptor mediaType is missing")
    if media_types is not None:
        require(value["mediaType"] in media_types, "unsupported OCI descriptor mediaType")
    return value


def verify_blob(layout, expected, metadata=False):
    descriptor(expected)
    digest = expected["digest"]
    path = "blobs/sha256/" + digest.split(":", 1)[1]
    if metadata:
        require(expected["size"] <= METADATA_LIMIT, "OCI metadata exceeds size limit")
    if metadata:
        data = _metadata_bytes(layout, path, digest)
        require(len(data) == expected["size"], "OCI blob size differs: %s" % digest)
        return parse_json(data)
    record = file_record(layout, path)
    require("sha256:" + record["sha256"] == digest, "OCI blob digest differs: %s" % digest)
    require((Path(layout) / path).stat().st_size == expected["size"],
            "OCI blob size differs: %s" % digest)
    return None


def _document(layout, expected):
    value = verify_blob(layout, expected, metadata=True)
    require(type(value) is dict and type(value.get("schemaVersion")) is int and
            value["schemaVersion"] == 2, "unsupported OCI document schemaVersion")
    require(value.get("mediaType") == expected["mediaType"], "OCI document mediaType differs")
    return value


def inspect(layout, expected_digest):
    """Verify the selected linux/amd64 manifest, config, and all compressed layers."""
    layout = Path(layout).resolve()
    digest_value(expected_digest, "expected OCI root digest", oci=True)
    header = parse_json(_metadata_bytes(layout, "oci-layout"))
    require(type(header) is dict and header.get("imageLayoutVersion") == "1.0.0",
            "unsupported OCI layout version")
    # index.json is the layout entry point, not an authority for the requested digest.
    index = parse_json(_metadata_bytes(layout, "index.json"))
    require(type(index) is dict and type(index.get("schemaVersion")) is int and
            index["schemaVersion"] == 2 and index.get("mediaType") == INDEX and
            type(index.get("manifests")) is list, "invalid OCI layout index")
    path = "blobs/sha256/" + expected_digest.split(":", 1)[1]
    root_bytes = _metadata_bytes(layout, path, expected_digest)
    size = len(root_bytes)
    root = parse_json(root_bytes)
    require(type(root) is dict, "OCI root must be an object")
    current = descriptor({"digest": expected_digest, "size": size,
                          "mediaType": root.get("mediaType")}, {INDEX, MANIFEST})
    depth = 0
    while current["mediaType"] == INDEX:
        require(depth < 8, "OCI index nesting exceeds limit")
        depth += 1
        document = _document(layout, current)
        manifests = document.get("manifests")
        require(type(manifests) is list and manifests, "OCI index manifests must be nonempty")
        candidates = []
        for value in manifests:
            descriptor(value, {INDEX, MANIFEST})
            platform = value.get("platform", {})
            require(type(platform) is dict, "OCI descriptor platform must be an object")
            if value["mediaType"] == INDEX and not platform:
                candidates.append(value)
            elif platform.get("os") == "linux" and platform.get("architecture") == "amd64":
                require(not platform.get("variant"), "unsupported amd64 platform variant")
                candidates.append(value)
        require(len(candidates) == 1, "OCI index must select exactly one linux/amd64 manifest")
        current = candidates[0]
    manifest = _document(layout, current)
    config_descriptor = descriptor(manifest.get("config"), {CONFIG})
    config = verify_blob(layout, config_descriptor, metadata=True)
    require(type(config) is dict and config.get("os") == "linux" and
            config.get("architecture") == "amd64" and not config.get("variant"),
            "component image config must be linux/amd64")
    layers = manifest.get("layers")
    require(type(layers) is list, "OCI layers must be an array")
    rootfs = config.get("rootfs")
    require(type(rootfs) is dict and rootfs.get("type") == "layers" and
            type(rootfs.get("diff_ids")) is list and len(rootfs["diff_ids"]) == len(layers),
            "OCI rootfs diff_ids do not cover the layers")
    for diff_id in rootfs["diff_ids"]:
        digest_value(diff_id, "OCI uncompressed layer digest", oci=True)
    for layer in layers:
        descriptor(layer, LAYERS)
        verify_blob(layout, layer)
    return {"kind": "crossforge-oci-layout-observation", "schema_version": 1,
            "root_digest": expected_digest, "platform_digest": current["digest"],
            "config_digest": config_descriptor["digest"], "platform": "linux/amd64",
            "layer_digests": [layer["digest"] for layer in layers],
            "compressed_layer_bytes": sum(layer["size"] for layer in layers)}


def metadata_graph(reference, copies, destination, frontend):
    """Produce a COPY-only graph; BuildKit applies layer and link semantics."""
    require(type(reference) is str and reference.count("@") == 1 and
            not any(ord(c) < 32 or ord(c) == 127 for c in reference),
            "component context must be digest-pinned")
    digest_value(reference.rsplit("@", 1)[1], "component context digest", oci=True)
    require(reference.startswith(("oci-layout://", "docker-image://")), "unsupported component context")
    require(reference.split("://", 1)[1].split("@", 1)[0], "component context location is missing")
    require(type(frontend) is str and re.fullmatch(
        r"docker/dockerfile:[a-zA-Z0-9_.-]+@sha256:[0-9a-f]{64}", frontend),
            "Dockerfile frontend must be pinned")
    require(type(copies) is dict and copies, "declare component metadata paths")
    for source, target in copies.items():
        relative_path(source)
        relative_path(target)
    lines = ["# syntax=" + frontend, "FROM scratch"]
    destinations = set()
    for source, target in sorted(copies.items()):
        relative_path(source)
        relative_path(target)
        require(source.startswith("component/"), "metadata source must be under component/")
        require(target not in destinations, "duplicate metadata destination")
        destinations.add(target)
        lines.append("COPY --from=component " + json.dumps(["/" + source, "/" + target]))
    return {"target": {"component-metadata": {
        "dockerfile-inline": "\n".join(lines) + "\n", "contexts": {"component": reference},
        "platforms": ["linux/amd64"],
        "output": [{"type": "local", "dest": str(Path(destination).resolve())}]}}}
