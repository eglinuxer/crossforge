"""Digest-preserving OCI transfer through a pinned upstream ORAS executable.

Transport does not establish provenance or qualification. The higher-level CLI
verifies trusted receipts and independently captured inputs on both boundaries.
"""

import hashlib
import io
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import urllib.request

from . import oci_layout
from .identity import digest_value, exact_fields, file_record, require


def validate_tool(policy):
    exact_fields(policy, ("schema_version", "name", "version", "platform", "url", "archive_sha256",
                          "binary_sha256", "git_commit", "member"), "OCI transfer tool policy")
    require(type(policy["schema_version"]) is int and policy["schema_version"] == 1,
            "unsupported OCI transfer tool policy version")
    require(policy["name"] == "oras" and policy["platform"] == "linux/amd64" and policy["member"] == "oras",
            "unsupported OCI transfer tool")
    require(type(policy["version"]) is str and re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", policy["version"]),
            "invalid ORAS version")
    require(policy["url"] == "https://github.com/oras-project/oras/releases/download/v%s/oras_%s_linux_amd64.tar.gz" %
            (policy["version"], policy["version"]), "ORAS download URL differs from pinned release")
    for field in ("archive_sha256", "binary_sha256"):
        digest_value(policy[field], field)
    require(type(policy["git_commit"]) is str and re.fullmatch(r"[0-9a-f]{40}", policy["git_commit"]),
            "ORAS source commit is invalid")
    return policy


def install_tool(policy, directory, archive=None):
    """Extract just the pinned regular binary; never apply archive filesystem entries."""
    validate_tool(policy)
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "ORAS output directory must be new")
    if archive is None:
        with urllib.request.urlopen(policy["url"], timeout=60) as response:
            data = response.read(32 * 1024 * 1024 + 1)
    else:
        with Path(archive).open("rb") as stream:
            data = stream.read(32 * 1024 * 1024 + 1)
    require(len(data) <= 32 * 1024 * 1024 and hashlib.sha256(data).hexdigest() == policy["archive_sha256"],
            "ORAS archive differs from pinned SHA256")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
        members = [member for member in bundle.getmembers() if member.name == policy["member"]]
        require(len(members) == 1 and members[0].isfile() and members[0].size <= 64 * 1024 * 1024,
                "ORAS archive binary must be a unique regular file")
        binary = bundle.extractfile(members[0]).read(64 * 1024 * 1024 + 1)
    require(hashlib.sha256(binary).hexdigest() == policy["binary_sha256"], "ORAS binary differs from pinned SHA256")
    directory.mkdir(parents=True)
    destination = directory / "oras"
    with destination.open("xb") as stream:
        stream.write(binary)
    destination.chmod(0o755)
    return destination


def _command(binary, policy):
    validate_tool(policy)
    binary = Path(binary).absolute()
    record = file_record(binary.parent, binary.name)
    require(record["sha256"] == policy["binary_sha256"] and int(record["mode"], 8) & 0o111,
            "ORAS executable differs from pinned SHA256 or is not executable")
    return [str(binary)]


def repository(value, loopback_http=False):
    require(type(value) is str and len(value) <= 255 and "/" in value, "explicit registry/repository is required")
    host, path = value.split("/", 1)
    require(re.fullmatch(r"(?:localhost|[a-z0-9]+(?:[.-][a-z0-9]+)+)(?::[1-9][0-9]{0,4})?", host) and
            re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*", path),
            "registry/repository is invalid or contains a tag, digest, credentials, or URL scheme")
    if ":" in host:
        require(int(host.rsplit(":", 1)[1]) <= 65535, "registry port is invalid")
    if loopback_http:
        require(host.split(":", 1)[0] in ("localhost", "127.0.0.1"), "plain HTTP is restricted to local loopback tests")
    return value


def reference(value, loopback_http=False):
    require(type(value) is str and value.count("@") == 1, "registry reference must contain an exact digest")
    repo, digest = value.split("@")
    return repository(repo, loopback_http), digest_value(digest, "registry artifact digest", oci=True)


def publish(layout, digest, destination, binary, policy, registry_config=None, loopback_http=False):
    destination = repository(destination, loopback_http)
    observation = oci_layout.inspect(layout, digest)
    command = _command(binary, policy)
    tag = destination + ":artifact-" + digest.split(":", 1)[1]
    arguments = command + ["cp", "--from-oci-layout"]
    if registry_config is not None:
        arguments += ["--to-registry-config", str(registry_config)]
    if loopback_http:
        arguments += ["--to-plain-http"]
    subprocess.run(arguments + [str(Path(layout).resolve()) + "@" + digest, tag], stdout=sys.stderr, check=True)
    remote = destination + "@" + digest
    arguments = command + ["manifest", "fetch"]
    if registry_config is not None:
        arguments += ["--registry-config", str(registry_config)]
    if loopback_http:
        arguments += ["--plain-http"]
    actual = subprocess.check_output(arguments + [remote])
    # ORAS fetch writes the original manifest bytes; no Docker exporter rebuilds
    # or recompresses the already sealed artifact during transfer.
    require("sha256:" + hashlib.sha256(actual).hexdigest() == digest, "published manifest bytes differ")
    return {"reference": remote, "retention_tag": tag, "artifact": {
        key: observation[key] for key in ("root_digest", "platform_digest", "config_digest", "platform")}}


def fetch(remote, directory, binary, policy, registry_config=None, loopback_http=False):
    repo, digest = reference(remote, loopback_http)
    directory = Path(directory).absolute()
    require(not directory.exists() and not directory.is_symlink(), "OCI download directory must be new")
    command = _command(binary, policy) + ["cp", "--to-oci-layout"]
    if registry_config is not None:
        command += ["--from-registry-config", str(registry_config)]
    if loopback_http:
        command += ["--from-plain-http"]
    directory.mkdir(parents=True)
    subprocess.run(command + [repo + "@" + digest, str(directory) + ":component"], stdout=sys.stderr, check=True)
    return oci_layout.inspect(directory, digest)
