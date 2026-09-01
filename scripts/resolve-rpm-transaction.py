#!/usr/bin/env python3
"""Resolve and attest a canonical RPM transaction with Rocky's DNF stack.

This is a maintenance-time tool.  It must run inside the Rocky image pinned by
release.json.  DNF selects packages, but this program independently verifies
the signed repository snapshot and every downloaded RPM before emitting a
deterministic transaction document.
"""

import argparse
import bz2
import gzip
import hashlib
import json
import lzma
import os
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
RELEASE_SCHEMA = REPOSITORY / "config/schemas/release.schema.json"
TRANSACTION_SCHEMA_URL = (
    "https://crossforge.dev/schemas/rpm-transaction.schema.json"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TARGET_TRIPLES = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
FORBIDDEN_PACKAGES = {
    "binutils",
    "gcc",
    "glibc-static",
    "libstdc++-devel",
    "libstdc++-static",
}


class ResolutionError(RuntimeError):
    pass


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(arguments, label, environment=None):
    process = subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=environment,
    )
    if process.returncode != 0:
        detail = (process.stdout + process.stderr).strip()
        raise ResolutionError("%s failed: %s" % (label, detail))
    return process.stdout


def strict_tools():
    path = REPOSITORY / "scripts/validate-release.py"
    if not path.is_file():
        raise ResolutionError("strict JSON validator is missing: %s" % path)
    return runpy.run_path(str(path))


def load_and_validate(path, schema_path, tools):
    document = tools["load_json"](path)
    schema = tools["load_json"](schema_path)
    tools["validate_schema_subset"](schema)
    tools["validate"](document, schema, schema, "$")
    return document


def ensure_safe_new_path(path, label):
    if path.is_symlink() or path.exists():
        raise ResolutionError("%s already exists or is a symlink: %s" % (label, path))
    parent = path.parent.resolve()
    if not parent.is_absolute() or str(parent) == "/":
        raise ResolutionError("unsafe %s parent: %s" % (label, parent))
    parent.mkdir(parents=True, exist_ok=True)


def safe_relative_path(value, label):
    path = Path(value)
    if (
        not value
        or value.startswith("/")
        or ".." in path.parts
        or any(part in ("", ".") for part in path.parts)
    ):
        raise ResolutionError("unsafe %s: %r" % (label, value))
    return path


def repository_relative(path, label):
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(REPOSITORY)
    except ValueError:
        raise ResolutionError("%s is outside the repository: %s" % (label, path))
    return str(safe_relative_path(str(relative), label))


def atomic_write_text(path, text):
    ensure_safe_new_path(path, "output")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(path))
    except Exception:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        raise


def staging_directory(destination, label):
    ensure_safe_new_path(destination, label)
    path = Path(
        tempfile.mkdtemp(
            prefix=".%s." % destination.name,
            dir=str(destination.parent),
        )
    )
    return path


def publish_directory(staging, destination):
    os.chmod(str(staging), 0o755)
    os.replace(str(staging), str(destination))


def download_file(url, destination, expected_sha256=None, expected_size=None,
                  maximum_size=None):
    if destination.is_symlink() or destination.exists():
        raise ResolutionError("download destination already exists: %s" % destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "crossforge-rpm-resolver/1"}
    )
    temporary = None
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    length = int(content_length)
                except ValueError:
                    raise ResolutionError("invalid Content-Length for %s" % url)
                if expected_size is not None and length != expected_size:
                    raise ResolutionError("Content-Length differs from repomd: %s" % url)
                if maximum_size is not None and length > maximum_size:
                    raise ResolutionError("download is larger than allowed: %s" % url)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=destination.name + ".",
                suffix=".part",
                dir=str(destination.parent),
                delete=False,
            ) as output:
                temporary = Path(output.name)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    limit = expected_size if expected_size is not None else maximum_size
                    if limit is not None and size > limit:
                        raise ResolutionError("oversized download: %s" % url)
        if expected_size is not None and size != expected_size:
            raise ResolutionError("download size differs from repomd: %s" % url)
        actual_sha256 = digest.hexdigest()
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise ResolutionError("download SHA256 differs from repomd: %s" % url)
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(destination))
    except Exception:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        raise
    return {"sha256": digest.hexdigest(), "size": size}


def copy_verified_file(source, destination):
    if source.is_symlink() or not source.is_file():
        raise ResolutionError("downloaded package is not a regular file: %s" % source)
    if destination.is_symlink() or destination.exists():
        raise ResolutionError("duplicate RPM output filename: %s" % destination.name)
    temporary = None
    try:
        with source.open("rb") as input_stream, tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=destination.name + ".",
            suffix=".part",
            dir=str(destination.parent),
            delete=False,
        ) as output_stream:
            temporary = Path(output_stream.name)
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(destination))
    except Exception:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        raise


def key_fingerprint(key_path):
    output = command(
        ["gpg", "--batch", "--show-keys", "--with-colons", str(key_path)],
        "RPM key inspection",
    )
    fingerprints = [
        line.split(":")[9].lower()
        for line in output.splitlines()
        if line.startswith("fpr:") and len(line.split(":")) > 9
    ]
    if len(fingerprints) != 1 or not FINGERPRINT_PATTERN.match(fingerprints[0]):
        raise ResolutionError("RPM key must contain exactly one primary fingerprint")
    return fingerprints[0]


def verify_detached_signature(key, fingerprint, signature, content, work):
    if work.is_symlink():
        raise ResolutionError("GnuPG work directory is a symlink: %s" % work)
    work.mkdir(exist_ok=True)
    if not work.is_dir():
        raise ResolutionError("invalid GnuPG work directory: %s" % work)
    homedir = work / "gnupg"
    homedir.mkdir(mode=0o700)
    command(
        ["gpg", "--batch", "--homedir", str(homedir), "--import", str(key)],
        "repository key import",
    )
    output = command(
        [
            "gpg",
            "--batch",
            "--homedir",
            str(homedir),
            "--status-fd",
            "1",
            "--verify",
            str(signature),
            str(content),
        ],
        "repomd signature verification",
    )
    valid = []
    for line in output.splitlines():
        if line.startswith("[GNUPG:] VALIDSIG "):
            fields = line.split()
            if len(fields) >= 3:
                valid.append(fields[2].lower())
    if valid != [fingerprint]:
        raise ResolutionError("repomd signature is not from the locked Rocky key")


def find_cached_repomd(repo):
    try:
        cache = Path(repo._repo.getCachedir())
    except (AttributeError, RuntimeError) as error:
        raise ResolutionError("cannot locate DNF repository cache: %s" % error)
    candidates = [
        path for path in cache.rglob("repomd.xml")
        if path.is_file() and not path.is_symlink()
    ]
    if len(candidates) != 1:
        raise ResolutionError(
            "DNF cache must contain exactly one repomd.xml for %s" % repo.id
        )
    return candidates[0]


def checksum_object(algorithm, value):
    return {"algorithm": algorithm, "value": value}


def parse_repomd(path):
    namespace = {"repo": "http://linux.duke.edu/metadata/repo"}
    try:
        root = ElementTree.parse(str(path)).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ResolutionError("cannot parse repomd.xml: %s" % error)
    revision = root.find("repo:revision", namespace)
    records = []
    seen_types = set()
    seen_locations = set()
    for element in root.findall("repo:data", namespace):
        metadata_type = element.get("type")
        checksum = element.find("repo:checksum", namespace)
        open_checksum = element.find("repo:open-checksum", namespace)
        location = element.find("repo:location", namespace)
        size = element.find("repo:size", namespace)
        open_size = element.find("repo:open-size", namespace)
        if (
            not metadata_type
            or metadata_type in seen_types
            or checksum is None
            or checksum.get("type") != "sha256"
            or not checksum.text
            or not SHA256_PATTERN.match(checksum.text)
            or location is None
            or not location.get("href")
            or size is None
            or not size.text
        ):
            raise ResolutionError("invalid or duplicate repomd metadata record")
        relative = str(safe_relative_path(location.get("href"), "metadata location"))
        if relative in seen_locations:
            raise ResolutionError("duplicate repomd metadata location: %s" % relative)
        try:
            compressed_size = int(size.text)
            expanded_size = int(open_size.text) if open_size is not None else None
        except (TypeError, ValueError):
            raise ResolutionError("invalid repomd metadata size: %s" % relative)
        if compressed_size <= 0 or (expanded_size is not None and expanded_size < 0):
            raise ResolutionError("invalid repomd metadata size: %s" % relative)
        expanded_checksum = None
        if open_checksum is not None:
            if (
                open_checksum.get("type") != "sha256"
                or not open_checksum.text
                or not SHA256_PATTERN.match(open_checksum.text)
            ):
                raise ResolutionError("invalid open checksum for %s" % relative)
            expanded_checksum = checksum_object("sha256", open_checksum.text)
        records.append(
            {
                "type": metadata_type,
                "location": relative,
                "checksum": checksum_object("sha256", checksum.text),
                "size": compressed_size,
                "open_checksum": expanded_checksum,
                "open_size": expanded_size,
            }
        )
        seen_types.add(metadata_type)
        seen_locations.add(relative)
    required = {"primary", "filelists", "primary_db"}
    missing = sorted(required - seen_types)
    if missing:
        raise ResolutionError("repomd is missing metadata: %s" % ", ".join(missing))
    return (
        revision.text if revision is not None and revision.text else None,
        sorted(records, key=lambda item: (item["type"], item["location"])),
    )


def expanded_stream(path):
    name = path.name
    if name.endswith(".gz"):
        return gzip.open(str(path), "rb")
    if name.endswith(".xz"):
        return lzma.open(str(path), "rb")
    if name.endswith(".bz2"):
        return bz2.open(str(path), "rb")
    if name.endswith((".zck", ".zst")):
        raise ResolutionError("unsupported metadata compression: %s" % name)
    return path.open("rb")


def verify_open_metadata(path, record):
    expected_checksum = record["open_checksum"]
    expected_size = record["open_size"]
    if expected_checksum is None and expected_size is None:
        return
    if expected_checksum is None or expected_size is None:
        raise ResolutionError("incomplete open metadata identity: %s" % path.name)
    digest = hashlib.sha256()
    size = 0
    try:
        with expanded_stream(path) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                if size > expected_size:
                    raise ResolutionError("expanded metadata is oversized: %s" % path)
    except (OSError, EOFError, lzma.LZMAError) as error:
        raise ResolutionError("cannot expand metadata %s: %s" % (path, error))
    if size != expected_size or digest.hexdigest() != expected_checksum["value"]:
        raise ResolutionError("expanded metadata differs from repomd: %s" % path)


def repository_evidence(repo, baseurl, key, fingerprint, destination, work):
    cached_repomd = find_cached_repomd(repo)
    repository_dir = destination / repo.id
    repository_dir.mkdir()
    repomd_destination = repository_dir / "repodata/repomd.xml"
    repomd_destination.parent.mkdir()
    copy_verified_file(cached_repomd, repomd_destination)
    signature_destination = repository_dir / "repodata/repomd.xml.asc"
    signature_result = download_file(
        urllib.parse.urljoin(baseurl, "repodata/repomd.xml.asc"),
        signature_destination,
        maximum_size=1024 * 1024,
    )
    verify_detached_signature(
        key,
        fingerprint,
        signature_destination,
        repomd_destination,
        work / ("gpg-" + repo.id),
    )
    revision, metadata = parse_repomd(repomd_destination)
    for record in metadata:
        relative = safe_relative_path(record["location"], "metadata location")
        output = repository_dir / relative
        result = download_file(
            urllib.parse.urljoin(baseurl, record["location"]),
            output,
            expected_sha256=record["checksum"]["value"],
            expected_size=record["size"],
        )
        if result["sha256"] != record["checksum"]["value"]:
            raise ResolutionError("metadata checksum mismatch: %s" % record["location"])
        verify_open_metadata(output, record)
    return {
        "id": repo.id,
        "baseurl": baseurl,
        "gpgcheck": True,
        "repo_gpgcheck": True,
        "gpg_key": {
            "sha256": sha256_file(key),
            "fingerprint": fingerprint,
        },
        "repomd": {
            "location": "repodata/repomd.xml",
            "sha256": sha256_file(repomd_destination),
            "size": repomd_destination.stat().st_size,
            "signature": {
                "location": "repodata/repomd.xml.asc",
                "sha256": signature_result["sha256"],
                "size": signature_result["size"],
                "fingerprint": fingerprint,
            },
            "revision": revision,
        },
        "metadata": metadata,
    }


def rpm_versions():
    query = "%{NAME}\\t%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}\\t%{ARCH}\\n"
    output = command(
        [
            "rpm",
            "-q",
            "--qf",
            query,
            "dnf",
            "python3-dnf",
            "libdnf",
            "libsolv",
            "rpm",
            "python3-rpm",
            "librepo",
        ],
        "resolver component query",
    )
    components = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 5:
            raise ResolutionError("unexpected resolver component query")
        name, epoch, version, release, arch = fields
        components.append(
            {
                "name": name,
                "nevra": "%s-%s:%s-%s.%s" % (
                    name, epoch, version, release, arch
                ),
            }
        )
    return sorted(components, key=lambda item: item["name"])


def read_os_release():
    values = {}
    path = Path("/etc/os-release")
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"')
    except OSError as error:
        raise ResolutionError("cannot read /etc/os-release: %s" % error)
    return values


def validate_environment(plan, release):
    os_release = read_os_release()
    identity = plan["identity"]
    if os_release.get("ID") != identity["distribution"]:
        raise ResolutionError("resolver distribution differs from the plan")
    if os_release.get("VERSION_ID") != identity["release"]:
        raise ResolutionError("resolver release differs from the plan")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ResolutionError("resolver must run on linux/x86_64")
    base_image = release["base_image"]
    if base_image["repository"] != "quay.io/rockylinux/rockylinux":
        raise ResolutionError("release base image is not Rocky Linux")


def validate_plan_semantics(plan):
    if plan.get("kind") != "rpm-plan":
        raise ResolutionError("plan kind must be rpm-plan")
    identity = plan["identity"]
    arch = identity["arch"]
    role = identity["role"]
    triple = identity["target_triple"]
    if role == "target-sysroot":
        expected = TARGET_TRIPLES.get(arch)
        if expected is None or triple != expected:
            raise ResolutionError("target sysroot triple differs from its architecture")
    elif triple is not None:
        raise ResolutionError("host RPM plans must use a null target_triple")
    policy = plan["solver_policy"]
    expected_arches = [arch, "noarch"]
    if policy["allowed_arches"] != expected_arches:
        raise ResolutionError(
            "allowed_arches must be exactly %r" % expected_arches
        )
    if (
        policy["install_weak_deps"] is not False
        or policy["best"] is not True
        or policy["strict"] is not True
        or policy["allow_erasing"] is not False
    ):
        raise ResolutionError("unsupported solver policy")
    repository_ids = [item["id"] for item in plan["repositories"]]
    if len(repository_ids) != len(set(repository_ids)):
        raise ResolutionError("duplicate repository id")
    for repository in plan["repositories"]:
        if not repository["baseurl"].startswith("https://"):
            raise ResolutionError("repository baseurl must use HTTPS")
        if not repository["baseurl"].endswith("/"):
            raise ResolutionError("repository baseurl must end in /")
    root_keys = [(item["name"], item["arch"]) for item in plan["roots"]]
    if len(root_keys) != len(set(root_keys)):
        raise ResolutionError("duplicate root request")
    if role == "target-sysroot":
        for root in plan["roots"]:
            if root["name"] in FORBIDDEN_PACKAGES:
                raise ResolutionError("forbidden RPM root: %s" % root["name"])
    base = plan["base"]
    if base["mode"] == "lock":
        if not base["parent_lock"] or not base["parent_sha256"]:
            raise ResolutionError("lock base requires parent_lock and parent_sha256")
    elif base["parent_lock"] is not None or base["parent_sha256"] is not None:
        raise ResolutionError("non-lock base must not name a parent lock")


def package_identity(package):
    epoch = int(package.epoch or 0)
    return {
        "name": package.name,
        "epoch": epoch,
        "version": package.version,
        "release": package.release,
        "arch": package.arch,
        "nevra": "%s-%d:%s-%s.%s" % (
            package.name,
            epoch,
            package.version,
            package.release,
            package.arch,
        ),
    }


def inventory(packages):
    records = [package_identity(package) for package in packages]
    records.sort(key=lambda item: item["nevra"])
    nevras = [item["nevra"] for item in records]
    if len(nevras) != len(set(nevras)):
        raise ResolutionError("RPM inventory contains duplicate NEVRA")
    return records


def manifest(nevras):
    packages = sorted(set(nevras))
    if len(packages) != len(nevras):
        raise ResolutionError("manifest contains duplicate NEVRA")
    return {
        "packages": packages,
        "canonical_sha256": canonical_sha256(packages),
    }


def load_parent_inventory(plan, tools):
    base = plan["base"]
    if base["mode"] != "lock":
        return None
    relative = safe_relative_path(base["parent_lock"], "parent lock path")
    path = (REPOSITORY / relative).resolve()
    if REPOSITORY not in path.parents:
        raise ResolutionError("parent lock escapes the repository")
    parent_lock = tools["load_json"](path)
    digest = canonical_sha256(parent_lock)
    if digest != base["parent_sha256"]:
        raise ResolutionError("parent lock canonical SHA256 differs from the plan")
    lock_schema_path = REPOSITORY / "config/schemas/rpm-lock.schema.json"
    if lock_schema_path.is_file():
        schema = tools["load_json"](lock_schema_path)
        tools["validate_schema_subset"](schema)
        tools["validate"](parent_lock, schema, schema, "$")
    try:
        transaction_reference = parent_lock["transaction"]
        transaction_relative = safe_relative_path(
            transaction_reference["file"], "parent transaction path"
        )
        transaction_path = (REPOSITORY / transaction_relative).resolve()
        if REPOSITORY not in transaction_path.parents:
            raise ResolutionError("parent transaction escapes the repository")
        parent_transaction = tools["load_json"](transaction_path)
        if canonical_sha256(parent_transaction) != transaction_reference[
            "canonical_sha256"
        ]:
            raise ResolutionError("parent transaction canonical SHA256 is invalid")
        transaction_schema_path = (
            REPOSITORY / "config/schemas/rpm-transaction.schema.json"
        )
        if transaction_schema_path.is_file():
            schema = tools["load_json"](transaction_schema_path)
            tools["validate_schema_subset"](schema)
            tools["validate"](parent_transaction, schema, schema, "$")
        result = parent_transaction["manifests"]["result"]
        packages = result["packages"]
        manifest_digest = result["canonical_sha256"]
    except (KeyError, TypeError):
        raise ResolutionError("parent lock has no result manifest")
    if canonical_sha256(packages) != manifest_digest:
        raise ResolutionError("parent result manifest digest is invalid")
    if packages != sorted(packages) or len(packages) != len(set(packages)):
        raise ResolutionError("parent result manifest is not canonical")
    return packages


def import_dnf():
    try:
        import dnf
        import dnf.conf
        import dnf.module.module_base
        import dnf.repo
        import dnf.rpm
        import hawkey
        import libdnf.transaction
    except ImportError as error:
        raise ResolutionError(
            "Rocky platform Python with python3-dnf is required: %s" % error
        )
    return dnf, hawkey, libdnf.transaction


def action_mapping(transaction_module):
    names = (
        ("install", "TransactionItemAction_INSTALL"),
        ("upgrade", "TransactionItemAction_UPGRADE"),
        ("upgraded", "TransactionItemAction_UPGRADED"),
        ("remove", "TransactionItemAction_REMOVE"),
    )
    result = {}
    for label, attribute in names:
        if hasattr(transaction_module, attribute):
            result[getattr(transaction_module, attribute)] = label
    return result


def reason_mapping(transaction_module):
    names = (
        ("unknown", "TransactionItemReason_UNKNOWN"),
        ("dependency", "TransactionItemReason_DEPENDENCY"),
        ("user", "TransactionItemReason_USER"),
        ("clean", "TransactionItemReason_CLEAN"),
        ("weak-dependency", "TransactionItemReason_WEAK_DEPENDENCY"),
        ("group", "TransactionItemReason_GROUP"),
    )
    result = {}
    for label, attribute in names:
        if hasattr(transaction_module, attribute):
            result[getattr(transaction_module, attribute)] = label
    return result


def configure_base(plan, work, dnf):
    identity = plan["identity"]
    policy = plan["solver_policy"]
    installroot = work / "installroot"
    installroot.mkdir()
    cache = work / "cache"
    persist = work / "persist"
    logs = work / "logs"
    for directory in (cache, persist, logs):
        directory.mkdir()

    configuration = dnf.conf.Conf()
    configuration.installroot = (
        str(installroot) if plan["base"]["mode"] == "empty" else "/"
    )
    configuration.cachedir = str(cache)
    configuration.persistdir = str(persist)
    configuration.logdir = str(logs)
    configuration.plugins = False
    configuration.best = True
    configuration.strict = True
    configuration.install_weak_deps = False
    configuration.skip_if_unavailable = False
    configuration.multilib_policy = "best"
    configuration.module_platform_id = policy["module_platform_id"]
    configuration.keepcache = True
    configuration.retries = 3
    configuration.substitutions["releasever"] = identity["release"]
    configuration.substitutions["arch"] = identity["arch"]
    configuration.substitutions["basearch"] = dnf.rpm.basearch(identity["arch"])
    base = dnf.Base(conf=configuration)
    return base, installroot


def add_repositories(base, plan, key):
    repositories = {}
    key_url = "file://" + urllib.request.pathname2url(str(key.resolve()))
    for configured in sorted(plan["repositories"], key=lambda item: item["id"]):
        repo = base.repos.add_new_repo(
            configured["id"], base.conf, baseurl=[configured["baseurl"]]
        )
        repo.gpgcheck = True
        repo.repo_gpgcheck = True
        repo.gpgkey = [key_url]
        repo.skip_if_unavailable = False
        repo.deltarpm = False
        repo.enable()
        repositories[repo.id] = repo
    enabled = sorted(repo.id for repo in base.repos.iter_enabled())
    expected = sorted(repositories)
    if enabled != expected:
        raise ResolutionError("enabled repositories differ from the RPM plan")
    return repositories


def mark_roots(base, plan, hawkey):
    repo_ids = sorted(item["id"] for item in plan["repositories"])
    target_arch = plan["identity"]["arch"]
    for root in sorted(
        plan["roots"], key=lambda item: (item["name"], item["arch"], item["purpose"])
    ):
        selector = root["arch"]
        if selector == "target":
            spec = "%s.%s" % (root["name"], target_arch)
            forms = [hawkey.FORM_NA]
        elif selector == "noarch":
            spec = "%s.noarch" % root["name"]
            forms = [hawkey.FORM_NA]
        else:
            spec = root["name"]
            forms = [hawkey.FORM_NAME]
        base.install(
            spec,
            reponame=repo_ids,
            strict=True,
            forms=forms,
        )


def rpm_signature_database(work, key):
    database = work / "rpmdb"
    database.mkdir()
    command(["rpm", "--dbpath", str(database), "--initdb"], "RPM database init")
    command(
        ["rpm", "--dbpath", str(database), "--import", str(key)],
        "Rocky key import",
    )
    return database


def verify_downloaded_rpm(package, source, destination, database, fingerprint):
    identity = package_identity(package)
    checksum_type, repository_checksum = package.returnIdSum()
    if checksum_type != "sha256" or not SHA256_PATTERN.match(repository_checksum):
        raise ResolutionError("%s does not use a SHA256 repository checksum" % identity["nevra"])
    expected_size = int(package.downloadsize)
    if source.stat().st_size != expected_size:
        raise ResolutionError("download size differs from DNF metadata: %s" % identity["nevra"])
    if sha256_file(source) != repository_checksum:
        raise ResolutionError("download SHA256 differs from DNF metadata: %s" % identity["nevra"])
    copy_verified_file(source, destination)
    digest = sha256_file(destination)
    if digest != repository_checksum:
        raise ResolutionError("copied RPM differs from DNF metadata: %s" % identity["nevra"])

    signature_output = command(
        [
            "rpmkeys",
            "--dbpath",
            str(database),
            "--checksig",
            "--verbose",
            str(destination),
        ],
        "signature verification for %s" % identity["nevra"],
    ).lower()
    signature_lines = [
        line for line in signature_output.splitlines() if "signature" in line
    ]
    if (
        not signature_lines
        or (fingerprint not in signature_output and fingerprint[-8:] not in signature_output)
        or any(not line.rstrip().endswith(": ok") for line in signature_lines)
    ):
        raise ResolutionError("invalid Rocky signature: %s" % identity["nevra"])

    query = (
        "%{NAME}\\t%{EPOCHNUM}\\t%{VERSION}\\t%{RELEASE}\\t"
        "%{ARCH}\\t%{SOURCERPM}\\t%{SIZE}\\n"
    )
    fields = command(
        [
            "rpm",
            "--dbpath",
            str(database),
            "-qp",
            "--qf",
            query,
            str(destination),
        ],
        "header query for %s" % identity["nevra"],
    ).strip().split("\t")
    if len(fields) != 7:
        raise ResolutionError("unexpected RPM header: %s" % identity["nevra"])
    expected_header = [
        identity["name"],
        str(identity["epoch"]),
        identity["version"],
        identity["release"],
        identity["arch"],
    ]
    if fields[:5] != expected_header:
        raise ResolutionError("RPM header differs from DNF: %s" % identity["nevra"])
    source_rpm = fields[5]
    try:
        install_size = int(fields[6])
    except ValueError:
        raise ResolutionError("invalid RPM install size: %s" % identity["nevra"])
    if not source_rpm.endswith(".src.rpm") or install_size < 0:
        raise ResolutionError("invalid RPM source identity: %s" % identity["nevra"])
    location = str(safe_relative_path(package.location, "RPM location"))
    return dict(
        identity,
        repo_id=package.reponame,
        location=location,
        url=urllib.parse.urljoin(package.repo.baseurl[0], location),
        repository_checksum=checksum_object("sha256", repository_checksum),
        sha256=digest,
        size=expected_size,
        install_size=install_size,
        source_rpm=source_rpm,
        signing_key_fingerprint=fingerprint,
    )


def request_manifest(plan, base_records, result_records, install_nevras,
                     forward_reasons):
    base_nevras = {item["nevra"] for item in base_records}
    target_arch = plan["identity"]["arch"]
    requests = []
    for root in sorted(
        plan["roots"], key=lambda item: (item["name"], item["arch"], item["purpose"])
    ):
        selector = root["arch"]
        matches = []
        for package in result_records:
            if package["name"] != root["name"]:
                continue
            if selector == "target" and package["arch"] != target_arch:
                continue
            if selector == "noarch" and package["arch"] != "noarch":
                continue
            if selector == "any" and package["arch"] not in (target_arch, "noarch"):
                continue
            matches.append(package["nevra"])
        if len(matches) != 1:
            raise ResolutionError(
                "root request %s.%s resolves to %d result packages"
                % (root["name"], selector, len(matches))
            )
        resolved = matches[0]
        if resolved in install_nevras:
            disposition = "transaction"
            if (
                plan["base"]["mode"] == "empty"
                and "user" not in forward_reasons.get(resolved, set())
            ):
                raise ResolutionError(
                    "transaction root is not marked user: %s" % resolved
                )
        elif resolved in base_nevras:
            disposition = "base"
        else:
            raise ResolutionError("root is absent from base and transaction: %s" % resolved)
        requests.append(
            {
                "name": root["name"],
                "arch": selector,
                "purpose": root["purpose"],
                "resolved_nevra": resolved,
                "disposition": disposition,
            }
        )
    return requests


def normalized_transaction_items(transaction_items, install_records, remove_records,
                                 transaction_module, reason_names):
    forward_actions = {
        getattr(transaction_module, "TransactionItemAction_INSTALL", None): "install",
        getattr(transaction_module, "TransactionItemAction_UPGRADE", None): "upgrade",
    }
    backward_actions = {
        getattr(transaction_module, "TransactionItemAction_UPGRADED", None),
        getattr(transaction_module, "TransactionItemAction_REMOVE", None),
    }
    forward_actions.pop(None, None)
    backward_actions.discard(None)
    transaction_by_key = {}
    for item in transaction_items:
        identity = package_identity(item.pkg)
        if item.action in forward_actions:
            action = forward_actions[item.action]
        elif item.action in backward_actions:
            action = "remove"
        else:
            raise ResolutionError("unsupported libdnf transaction action")
        reason = reason_names.get(item.reason)
        if reason is None:
            raise ResolutionError("unsupported libdnf transaction reason")
        key = (identity["nevra"], action)
        if key in transaction_by_key:
            raise ResolutionError("duplicate normalized transaction item: %r" % (key,))
        transaction_by_key[key] = (item, reason)

    content_fields = (
        "repo_id",
        "location",
        "url",
        "repository_checksum",
        "size",
        "install_size",
        "source_rpm",
    )
    items = []
    forward_reasons = {}
    for nevra, verified in sorted(install_records.items()):
        candidates = [
            (key, value)
            for key, value in transaction_by_key.items()
            if key[0] == nevra and key[1] != "remove"
        ]
        if len(candidates) != 1:
            raise ResolutionError(
                "downloaded RPM has %d forward transaction items: %s"
                % (len(candidates), nevra)
            )
        key, (_transaction_item, reason) = candidates[0]
        record = {
            field: verified[field]
            for field in ("name", "epoch", "version", "release", "arch", "nevra")
        }
        for field in content_fields:
            record[field] = verified[field]
        record["action"] = key[1]
        record["reason"] = reason
        items.append(record)
        forward_reasons.setdefault(nevra, set()).add(reason)

    for removed in sorted(remove_records, key=lambda item: item["nevra"]):
        key = (removed["nevra"], "remove")
        value = transaction_by_key.get(key)
        if value is None:
            raise ResolutionError(
                "removed RPM has no backward transaction item: %s" % removed["nevra"]
            )
        transaction_item, reason = value
        record = dict(removed)
        record.update(
            {
                "repo_id": transaction_item.from_repo or None,
                "action": "remove",
                "reason": reason,
                "location": None,
                "url": None,
                "repository_checksum": None,
                "size": None,
                "install_size": None,
                "source_rpm": None,
            }
        )
        items.append(record)

    expected_keys = {(item["nevra"], item["action"]) for item in items}
    unexplained = sorted(set(transaction_by_key) - expected_keys)
    if unexplained:
        raise ResolutionError(
            "unexplained normalized transaction items: %r" % unexplained
        )
    items.sort(key=lambda item: (item["nevra"], item["action"]))
    return items, forward_reasons


def self_check_transaction(transaction):
    manifests = transaction["manifests"]
    manifest_sets = {}
    for name in ("base", "remove", "result"):
        current = manifests[name]
        packages = current["packages"]
        if packages != sorted(packages) or len(packages) != len(set(packages)):
            raise ResolutionError("%s manifest is not sorted and unique" % name)
        if canonical_sha256(packages) != current["canonical_sha256"]:
            raise ResolutionError("%s manifest digest is invalid" % name)
        manifest_sets[name] = set(packages)

    items = transaction["items"]
    item_keys = [(item["nevra"], item["action"]) for item in items]
    if item_keys != sorted(item_keys) or len(item_keys) != len(set(item_keys)):
        raise ResolutionError("transaction items are not sorted and unique")
    forward_actions = {"install", "upgrade"}
    forward = {
        item["nevra"] for item in items if item["action"] in forward_actions
    }
    removed = {item["nevra"] for item in items if item["action"] == "remove"}
    if removed != manifest_sets["remove"]:
        raise ResolutionError("remove items differ from the remove manifest")
    if not removed.issubset(manifest_sets["base"]):
        raise ResolutionError("remove manifest is not a subset of the base")
    expected_result = (manifest_sets["base"] - removed) | forward
    if expected_result != manifest_sets["result"]:
        raise ResolutionError("transaction algebra differs from the result manifest")

    repository_ids = {repository["id"] for repository in transaction["repositories"]}
    content_fields = (
        "repo_id",
        "location",
        "url",
        "repository_checksum",
        "size",
        "install_size",
        "source_rpm",
    )
    for item in items:
        values = [item[field] for field in content_fields]
        if item["action"] == "remove":
            if any(value is not None for value in values[1:]):
                raise ResolutionError("remove item contains repository content identity")
        else:
            if any(value is None for value in values):
                raise ResolutionError("forward item lacks repository content identity")
            if item["repo_id"] not in repository_ids:
                raise ResolutionError("forward item uses an undeclared repository")

    for request in transaction["requests"]:
        nevra = request["resolved_nevra"]
        if nevra not in manifest_sets["result"]:
            raise ResolutionError("request is absent from the result manifest")
        if request["disposition"] == "base" and nevra not in manifest_sets["base"]:
            raise ResolutionError("base request is absent from the base manifest")
        if request["disposition"] == "transaction" and nevra not in forward:
            raise ResolutionError("transaction request has no forward item")

    expected_load_system = transaction["base"]["mode"] != "empty"
    if transaction["resolver"]["load_system_repo"] is not expected_load_system:
        raise ResolutionError("resolver base mode and system-repo policy differ")


def resolve(arguments):
    tools = strict_tools()
    plan = load_and_validate(arguments.plan, arguments.plan_schema, tools)
    release = load_and_validate(arguments.release_config, RELEASE_SCHEMA, tools)
    validate_plan_semantics(plan)
    validate_environment(plan, release)

    trust = release["trust"]["rocky_rpm_key"]
    if sha256_file(arguments.key) != trust["sha256"]:
        raise ResolutionError("Rocky key SHA256 differs from release.json")
    fingerprint = key_fingerprint(arguments.key)
    if fingerprint != trust["fingerprint"]:
        raise ResolutionError("Rocky key fingerprint differs from release.json")

    parent_inventory = load_parent_inventory(plan, tools)
    dnf, hawkey, transaction_module = import_dnf()
    reason_names = reason_mapping(transaction_module)
    if not action_mapping(transaction_module) or not reason_names:
        raise ResolutionError("unsupported libdnf transaction API")

    rpm_stage = None
    metadata_stage = None
    work_context = None
    base = None
    try:
        rpm_stage = staging_directory(arguments.rpm_dir, "RPM output directory")
        metadata_stage = staging_directory(
            arguments.metadata_dir, "metadata output directory"
        )
        work_context = tempfile.TemporaryDirectory(
            prefix="crossforge-rpm-resolver-"
        )
        work = Path(work_context.name)
        base, installroot = configure_base(plan, work, dnf)
        repositories = add_repositories(base, plan, arguments.key)
        repository_records = []
        for configured in sorted(
            plan["repositories"], key=lambda item: item["id"]
        ):
            repo = repositories[configured["id"]]
            try:
                repo.load()
            except Exception as error:
                raise ResolutionError(
                    "cannot load signed repository %s: %s" % (repo.id, error)
                )
            repository_records.append(
                repository_evidence(
                    repo,
                    configured["baseurl"],
                    arguments.key,
                    fingerprint,
                    metadata_stage,
                    work,
                )
            )

        load_system_repo = plan["base"]["mode"] != "empty"
        try:
            base.fill_sack(load_system_repo=load_system_repo)
        except Exception as error:
            raise ResolutionError("DNF sack loading failed: %s" % error)
        installed_query = base.sack.query().installed()
        base_records = inventory(installed_query)
        if not load_system_repo and base_records:
            raise ResolutionError("empty resolver loaded installed RPMs")
        if load_system_repo and not base_records:
            raise ResolutionError("non-empty resolver did not load the image RPMDB")
        if plan["base"]["mode"] == "lock":
            current = [item["nevra"] for item in base_records]
            if current != parent_inventory:
                raise ResolutionError("current RPM inventory differs from the parent lock")

        enabled_modules = sorted(plan["solver_policy"]["enabled_modules"])
        if enabled_modules:
            try:
                module_base = dnf.module.module_base.ModuleBase(base)
                module_base.enable(enabled_modules)
            except Exception as error:
                raise ResolutionError("module enablement failed: %s" % error)

        allowed_arches = set(plan["solver_policy"]["allowed_arches"])
        disallowed = base.sack.query().available().filter(
            arch__neq=sorted(allowed_arches)
        )
        if disallowed:
            base.sack.add_excludes(disallowed)
        try:
            mark_roots(base, plan, hawkey)
            resolved = base.resolve(allow_erasing=False)
        except Exception as error:
            raise ResolutionError("DNF dependency resolution failed: %s" % error)
        if not resolved or base.transaction is None:
            # A no-op transaction is valid only when every root is already in the base.
            transaction_items = []
            install_packages = []
            remove_packages = []
        else:
            transaction_items = list(base.transaction)
            install_packages = list(base.transaction.install_set)
            remove_packages = list(base.transaction.remove_set)

        for package in install_packages:
            if package.arch not in allowed_arches:
                raise ResolutionError(
                    "DNF selected forbidden package architecture: %s" % package
                )
            if (
                plan["identity"]["role"] == "target-sysroot"
                and package.name in FORBIDDEN_PACKAGES
            ):
                raise ResolutionError("DNF selected forbidden package: %s" % package.name)
            if package.reponame not in repositories:
                raise ResolutionError("DNF selected package from an undeclared repository")

        try:
            if install_packages:
                base.download_packages(install_packages)
        except Exception as error:
            raise ResolutionError("DNF package download failed: %s" % error)

        signature_database = rpm_signature_database(work, arguments.key)
        installed_records = {}
        for package in sorted(install_packages, key=lambda item: package_identity(item)["nevra"]):
            identity = package_identity(package)
            source = Path(package.localPkg())
            destination = rpm_stage / Path(package.location).name
            record = verify_downloaded_rpm(
                package,
                source,
                destination,
                signature_database,
                fingerprint,
            )
            installed_records[identity["nevra"]] = record

        install_nevras = set(installed_records)
        remove_records = inventory(remove_packages)
        items, forward_reasons = normalized_transaction_items(
            transaction_items,
            installed_records,
            remove_records,
            transaction_module,
            reason_names,
        )
        remove_nevras = {item["nevra"] for item in remove_records}
        base_by_nevra = {item["nevra"]: item for item in base_records}
        result_by_nevra = {
            nevra: record
            for nevra, record in base_by_nevra.items()
            if nevra not in remove_nevras
        }
        for nevra, record in installed_records.items():
            result_by_nevra[nevra] = {
                key: record[key]
                for key in ("name", "epoch", "version", "release", "arch", "nevra")
            }
        result_records = sorted(
            result_by_nevra.values(), key=lambda item: item["nevra"]
        )
        requests = request_manifest(
            plan,
            base_records,
            result_records,
            install_nevras,
            forward_reasons,
        )
        if plan["base"]["mode"] == "empty":
            user_nevras = {
                item["nevra"]
                for item in items
                if item["reason"] == "user" and item["nevra"] in install_nevras
            }
            request_nevras = {
                item["resolved_nevra"]
                for item in requests
                if item["disposition"] == "transaction"
            }
            if user_nevras != request_nevras:
                raise ResolutionError(
                    "empty-base DNF user reasons differ from root requests"
                )
            invalid_reasons = {
                item["reason"]
                for item in items
                if item["nevra"] in install_nevras
            } - {"user", "dependency"}
            if invalid_reasons:
                raise ResolutionError(
                    "empty-base transaction has invalid reasons: %s"
                    % ", ".join(sorted(invalid_reasons))
                )

        base_manifest = manifest([item["nevra"] for item in base_records])
        remove_manifest = manifest([item["nevra"] for item in remove_records])
        result_manifest = manifest([item["nevra"] for item in result_records])
        release_base = release["base_image"]
        transaction = {
            "$schema": TRANSACTION_SCHEMA_URL,
            "schema_version": 1,
            "kind": "rpm-transaction",
            "identity": plan["identity"],
            "plan": {
                "file": repository_relative(arguments.plan, "plan path"),
                "canonical_sha256": canonical_sha256(plan),
            },
            "resolver": {
                "implementation": "python3-dnf",
                "contract_version": 1,
                "image": "%s:%s"
                % (release_base["repository"], release_base["tag"]),
                "image_digest": release_base["digest"],
                "components": rpm_versions(),
                "platform_python": platform.python_version(),
                "load_system_repo": load_system_repo,
                "plugins": False,
            },
            "base": plan["base"],
            "repositories": repository_records,
            "solver_policy": {
                "allowed_arches": plan["solver_policy"]["allowed_arches"],
                "install_weak_deps": False,
                "best": True,
                "strict": True,
                "allow_erasing": False,
                "module_platform_id": plan["solver_policy"]["module_platform_id"],
                "enabled_modules": enabled_modules,
            },
            "requests": requests,
            "items": items,
            "manifests": {
                "base": base_manifest,
                "remove": remove_manifest,
                "result": result_manifest,
            },
        }

        self_check_transaction(transaction)

        if installroot.exists() and plan["base"]["mode"] == "empty":
            unexpected = list(installroot.iterdir())
            if unexpected:
                raise ResolutionError("DNF mutated the empty installroot")

        transaction_schema_path = (
            REPOSITORY / "config/schemas/rpm-transaction.schema.json"
        )
        if transaction_schema_path.is_file():
            schema = tools["load_json"](transaction_schema_path)
            tools["validate_schema_subset"](schema)
            tools["validate"](transaction, schema, schema, "$")

        text = json.dumps(transaction, indent=2, ensure_ascii=False) + "\n"
        publish_directory(rpm_stage, arguments.rpm_dir)
        rpm_stage = None
        publish_directory(metadata_stage, arguments.metadata_dir)
        metadata_stage = None
        atomic_write_text(arguments.output, text)
        return transaction
    finally:
        if base is not None:
            try:
                base.close()
            except Exception:
                pass
        if work_context is not None:
            work_context.cleanup()
        if rpm_stage is not None and rpm_stage.exists():
            shutil.rmtree(str(rpm_stage))
        if metadata_stage is not None and metadata_stage.exists():
            shutil.rmtree(str(metadata_stage))


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-schema", type=Path, required=True)
    parser.add_argument(
        "--release-config",
        type=Path,
        default=REPOSITORY / "config/release.json",
    )
    parser.add_argument(
        "--key",
        type=Path,
        default=REPOSITORY / "keys/RPM-GPG-KEY-rockyofficial",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rpm-dir", type=Path, required=True)
    parser.add_argument("--metadata-dir", type=Path, required=True)
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    try:
        transaction = resolve(arguments)
    except (OSError, ResolutionError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "resolved: %s (%d transaction item(s); canonical sha256:%s)"
        % (
            arguments.output,
            len(transaction["items"]),
            canonical_sha256(transaction),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
