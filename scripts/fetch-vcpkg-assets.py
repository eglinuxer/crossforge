#!/usr/bin/env python3
"""Fetch or verify the explicit source-asset closure for a vcpkg gate."""

import argparse
import hashlib
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
COMPONENT_READER = runpy.run_path(
    str(SCRIPT_DIRECTORY / "release_component.py")
)
ComponentError = COMPONENT_READER["ComponentError"]
ASSET_FIELDS = {"filename", "sha256", "sha512", "size", "url"}
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
SHA512_RE = re.compile(r"^[0-9a-f]{128}\Z")


class AssetError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise AssetError(message)


def material_map(component):
    return {item["path"]: item["value"] for item in component["materials"]}


def asset_prefix(component):
    name = component.get("component", "")
    match = re.fullmatch(
        r"implementation/(vcpkg-upstream-tier[0-9]+)-qualification", name
    )
    require(match is not None, "unsupported vcpkg asset policy component")
    return "/@implementation/%s/assets/" % match.group(1)


def policy_assets(component):
    prefix = asset_prefix(component)
    records = {}
    for path, value in material_map(component).items():
        match = re.match(
            r"^%s([0-9]+)/(filename|sha256|sha512|size|url)$"
            % re.escape(prefix),
            path,
        )
        if match:
            records.setdefault(int(match.group(1)), {})[match.group(2)] = value
    require(records, "vcpkg asset policy is empty")
    require(
        sorted(records) == list(range(len(records))),
        "vcpkg asset policy indices are not contiguous",
    )
    assets = [records[index] for index in sorted(records)]
    for asset in assets:
        require(set(asset) == ASSET_FIELDS, "vcpkg asset fields differ")
        require(
            isinstance(asset["filename"], str)
            and FILENAME_RE.fullmatch(asset["filename"]) is not None,
            "vcpkg asset filename is unsafe",
        )
        require(
            isinstance(asset["url"], str)
            and asset["url"].startswith("https://")
            and not any(character.isspace() for character in asset["url"]),
            "vcpkg asset URL is unsafe",
        )
        require(
            isinstance(asset["sha256"], str)
            and SHA256_RE.fullmatch(asset["sha256"]) is not None,
            "vcpkg asset SHA256 is invalid",
        )
        require(
            isinstance(asset["sha512"], str)
            and SHA512_RE.fullmatch(asset["sha512"]) is not None,
            "vcpkg asset SHA512 is invalid",
        )
        require(
            type(asset["size"]) is int and asset["size"] > 0,
            "vcpkg asset size is invalid",
        )
    filenames = [asset["filename"] for asset in assets]
    require(
        filenames == sorted(filenames) and len(filenames) == len(set(filenames)),
        "vcpkg asset filenames are not sorted and unique",
    )
    return assets


def file_identity(path):
    sha256 = hashlib.sha256()
    sha512 = hashlib.sha512()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha256.update(block)
            sha512.update(block)
            size += len(block)
    return {
        "sha256": sha256.hexdigest(),
        "sha512": sha512.hexdigest(),
        "size": size,
    }


def verify_asset_root(root, assets):
    require(root.is_dir() and not root.is_symlink(), "asset root is unsafe")
    entries = list(root.iterdir())
    require(
        all(path.is_file() and not path.is_symlink() for path in entries),
        "asset root contains a non-regular entry",
    )
    require(
        sorted(path.name for path in entries)
        == [asset["filename"] for asset in assets],
        "asset root inventory differs",
    )
    for asset in assets:
        path = root / asset["filename"]
        require(
            file_identity(path)
            == {
                "sha256": asset["sha256"],
                "sha512": asset["sha512"],
                "size": asset["size"],
            },
            "asset identity differs: %s" % asset["filename"],
        )
    return assets


def fetch_assets(root, assets):
    require(not root.exists(), "asset output already exists")
    root.mkdir(parents=True)
    for asset in assets:
        destination = root / asset["filename"]
        temporary = destination.with_name(destination.name + ".tmp")
        process = subprocess.run(
            [
                "curl",
                "--fail",
                "--location",
                "--retry",
                "3",
                "--output",
                str(temporary),
                asset["url"],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        require(
            process.returncode == 0,
            "asset download failed: %s\n%s"
            % (asset["filename"], process.stdout + process.stderr),
        )
        require(
            file_identity(temporary)
            == {
                "sha256": asset["sha256"],
                "sha512": asset["sha512"],
                "size": asset["size"],
            },
            "downloaded asset identity differs: %s" % asset["filename"],
        )
        os.chmod(str(temporary), 0o644)
        temporary.replace(destination)
    return verify_asset_root(root, assets)


def load_policy(path, name, digest):
    try:
        component = COMPONENT_READER["load_component"](
            path,
            name,
            "qualification",
            digest,
        )
    except ComponentError as error:
        raise AssetError("invalid vcpkg asset policy: %s" % error) from error
    return policy_assets(component)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("mode", choices=("fetch", "verify"))
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--expected-component", required=True)
    parser.add_argument("--component-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    assets = load_policy(
        arguments.component,
        arguments.expected_component,
        arguments.component_sha256,
    )
    if arguments.mode == "fetch":
        fetch_assets(arguments.output, assets)
    else:
        verify_asset_root(arguments.output, assets)
    print("%s vcpkg assets: %d" % (arguments.mode, len(assets)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssetError, OSError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
