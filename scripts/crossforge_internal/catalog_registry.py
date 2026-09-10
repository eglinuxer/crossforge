"""Store signed catalogs durably; mutable input tags are discovery hints only."""

import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile

from . import component_artifacts, component_catalog, component_inputs, registry_transfer
from .identity import canonical_bytes, content_sha256, digest_value, exact_fields, parse_json, require


ARTIFACT_TYPE = "application/vnd.crossforge.component-catalog.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
TITLE = "org.opencontainers.image.title"
CREATED = "org.opencontainers.image.created"
FILES = {"catalog.json": ("application/json", 64 * 1024 * 1024),
         "catalog.sigstore.json": ("application/vnd.dev.sigstore.bundle.v0.3+json", 16 * 1024 * 1024)}
EMPTY_CONFIG = {"mediaType": "application/vnd.oci.empty.v1+json", "digest": "sha256:" + hashlib.sha256(b"{}").hexdigest(),
                "size": 2, "data": "e30="}


def input_tag(expected_inputs, role):
    component_inputs.validate(expected_inputs)
    require(role in component_artifacts.ROLES and expected_inputs["scope"] == (
        "qualification" if role == "qualification" else "build"), "catalog index role or scope differs")
    return "input-" + content_sha256({"component": expected_inputs["component"], "role": role,
                                     "inputs_sha256": component_inputs.identity(expected_inputs)})


def _repository(value, loopback_http):
    registry_transfer.repository(value, loopback_http)
    require(value == component_catalog.REPOSITORY or loopback_http, "catalog repository differs from policy")
    return value


def _options(registry_config, loopback_http):
    arguments = ["--registry-config", str(registry_config)] if registry_config is not None else []
    return arguments + (["--plain-http"] if loopback_http else [])


def _manifest(remote, binary, policy, registry_config, loopback_http, allow_missing=False):
    command = registry_transfer._command(binary, policy) + ["manifest", "fetch"] + _options(registry_config, loopback_http) + [remote]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if result.returncode:
        # ORAS 1.3.4 turns a manifest 404 into this exact contextual error.
        # Authentication, transport and all other failures remain errors.
        absent = 'Error response from registry: failed to fetch the content of "%s": %s: not found\n' % (remote, remote)
        if allow_missing and result.returncode == 1 and not result.stdout and result.stderr == absent.encode("utf-8"):
            return None
        sys.stderr.write(result.stderr.decode("utf-8", errors="replace")[:4096])
        raise subprocess.CalledProcessError(result.returncode, command)
    require(0 < len(result.stdout) <= 2 * 1024 * 1024, "catalog manifest exceeds size limit")
    return result.stdout


def manifest(data, digest=None):
    actual = "sha256:" + hashlib.sha256(data).hexdigest()
    if digest is not None:
        require(actual == digest_value(digest, "catalog manifest digest", oci=True), "catalog manifest digest differs")
    value = parse_json(data)
    exact_fields(value, ("schemaVersion", "mediaType", "artifactType", "config", "layers", "annotations"), "catalog OCI manifest")
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 2 and value["mediaType"] == MANIFEST_TYPE and
            value["artifactType"] == ARTIFACT_TYPE, "unsupported catalog OCI manifest")
    require(canonical_bytes(value["config"]) == canonical_bytes(EMPTY_CONFIG), "catalog OCI config differs")
    exact_fields(value["annotations"], (CREATED,), "catalog manifest annotations")
    component_artifacts._timestamp(value["annotations"][CREATED])
    require(type(value["layers"]) is list and len(value["layers"]) == 2, "catalog OCI layer count differs")
    layers = {}
    for layer in value["layers"]:
        exact_fields(layer, ("mediaType", "digest", "size", "annotations"), "catalog OCI layer")
        exact_fields(layer["annotations"], (TITLE,), "catalog layer annotations")
        name = layer["annotations"][TITLE]
        require(type(name) is str and name in FILES and name not in layers, "catalog OCI filenames differ")
        media_type, maximum = FILES[name]
        require(layer["mediaType"] == media_type and type(layer["size"]) is int and 0 < layer["size"] <= maximum,
                "catalog OCI layer type or size differs")
        digest_value(layer["digest"], "catalog layer digest", oci=True)
        layers[name] = layer
    return actual, layers


def _check_file(directory, name, descriptor):
    data = component_catalog.regular_bytes(Path(directory) / name, FILES[name][1])
    require(len(data) == descriptor["size"] and "sha256:" + hashlib.sha256(data).hexdigest() == descriptor["digest"],
            "catalog blob differs from manifest: " + name)
    return data


def pack(catalog, bundle, directory, binary, policy):
    """Transport-only packing; this function does not authenticate a signature."""
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "catalog packing directory must be new")
    data = component_catalog.regular_bytes(catalog, FILES["catalog.json"][1])
    value = component_catalog.validate(parse_json(data))
    require(data == canonical_bytes(value) + b"\n", "catalog encoding differs")
    signature = component_catalog.regular_bytes(bundle, FILES["catalog.sigstore.json"][1])
    directory.mkdir(parents=True)
    (directory / "catalog.json").write_bytes(data)
    (directory / "catalog.sigstore.json").write_bytes(signature)
    command = registry_transfer._command(binary, policy) + ["push", "--oci-layout", "layout:catalog", "--image-spec", "v1.1",
        "--artifact-type", ARTIFACT_TYPE, "--annotation", CREATED + "=" + value["producer"]["started_at"],
        "--export-manifest", "manifest.json"]
    command += [name + ":" + FILES[name][0] for name in sorted(FILES)]
    subprocess.run(command, cwd=str(directory), stdout=sys.stderr, check=True)
    raw = component_catalog.regular_bytes(directory / "manifest.json", 2 * 1024 * 1024)
    digest, layers = manifest(raw)
    for name, descriptor in layers.items():
        _check_file(directory, name, descriptor)
        blob = directory / "layout/blobs/sha256" / descriptor["digest"].split(":")[1]
        require(component_catalog.regular_bytes(blob, FILES[name][1]) == (directory / name).read_bytes(),
                "packed catalog blob differs")
    return {"digest": digest, "manifest": raw, "catalog": value}


def _publish_packed(directory, packed, repository, binary, policy, registry_config, loopback_http):
    """Copy an already checked envelope without signing or asserting trust."""
    _repository(repository, loopback_http)
    digest = packed["digest"]
    retention_tag = "catalog-" + digest.split(":")[1]
    command = registry_transfer._command(binary, policy) + ["cp", "--from-oci-layout"]
    if registry_config is not None:
        command += ["--to-registry-config", str(registry_config)]
    if loopback_http:
        command += ["--to-plain-http"]
    subprocess.run(command + [str(Path(directory) / "layout") + "@" + digest, repository + ":" + retention_tag],
                   stdout=sys.stderr, check=True)
    remote = repository + "@" + digest
    require(_manifest(remote, binary, policy, registry_config, loopback_http) == packed["manifest"],
            "published catalog manifest differs")
    return {"reference": remote, "retention_tag": repository + ":" + retention_tag}


def _download(repository, raw, directory, binary, policy, registry_config, loopback_http, digest=None):
    digest, layers = manifest(raw, digest)
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "catalog download directory must be new")
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_bytes(raw)
    for name, descriptor in sorted(layers.items()):
        command = registry_transfer._command(binary, policy) + ["blob", "fetch"] + _options(registry_config, loopback_http)
        subprocess.run(command + ["--output", str(directory / name), repository + "@" + descriptor["digest"]],
                       stdout=sys.stderr, check=True, timeout=120)
        _check_file(directory, name, descriptor)
    return {"reference": repository + "@" + digest, "catalog": str(directory / "catalog.json"),
            "bundle": str(directory / "catalog.sigstore.json")}


def publish(source, catalog, bundle, cosign, directory, repository, binary, policy, registry_config=None, loopback_http=False):
    _repository(repository, loopback_http)
    with tempfile.TemporaryDirectory(prefix="catalog-publish-") as temporary:
        snapshots = Path(temporary)
        for name, path in (("catalog.json", catalog), ("catalog.sigstore.json", bundle)):
            (snapshots / name).write_bytes(component_catalog.regular_bytes(path, FILES[name][1]))
        value, authentication = component_catalog.verify(source, snapshots / "catalog.json", snapshots / "catalog.sigstore.json", cosign)
        packed = pack(snapshots / "catalog.json", snapshots / "catalog.sigstore.json", directory, binary, policy)
    result = _publish_packed(directory, packed, repository, binary, policy, registry_config, loopback_http)
    tags = sorted({input_tag(entry["receipt"]["contract"]["inputs"], entry["receipt"]["contract"]["role"]) for entry in value["entries"]})
    command = registry_transfer._command(binary, policy) + ["tag"] + _options(registry_config, loopback_http)
    subprocess.run(command + [result["reference"]] + tags, stdout=sys.stderr, check=True)
    for tag in tags:
        require(_manifest(repository + ":" + tag, binary, policy, registry_config, loopback_http) == packed["manifest"],
                "catalog input index changed during publication")
    result.update(authentication=authentication, input_tags=tags)
    return result


def lookup(source, expected_inputs, role, cosign, directory, repository, binary, policy, registry_config=None,
           loopback_http=False, catalog_reference=None):
    _repository(repository, loopback_http)
    tag = input_tag(expected_inputs, role)
    digest = None
    remote = repository + ":" + tag
    if catalog_reference is not None:
        selected_repository, digest = registry_transfer.reference(catalog_reference, loopback_http)
        require(selected_repository == repository, "recovery catalog repository differs")
        remote = catalog_reference
    raw = _manifest(remote, binary, policy, registry_config, loopback_http, allow_missing=catalog_reference is None)
    if raw is None:
        return {"status": "missing", "reason": "catalog-index-absent", "input_tag": tag, "entry": None}
    downloaded = _download(repository, raw, directory, binary, policy, registry_config, loopback_http, digest)
    selected = component_catalog.select(source, downloaded["catalog"], downloaded["bundle"], cosign, expected_inputs, role)
    require(selected["status"] == "authenticated-reference", "catalog index or recovery reference does not cover expected inputs")
    result = copy.deepcopy(selected)
    result.update(catalog=downloaded, input_tag=tag)
    return result
