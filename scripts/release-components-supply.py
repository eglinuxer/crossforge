#!/usr/bin/env python3
"""Supply and candidate policy for the complete release-component graph."""


CANDIDATE_MANIFEST_POLICY = {
    "schema_version": 1,
    "schema": "https://crossforge.dev/schemas/candidate.schema.json",
    "kind": "crossforge-candidate",
    "identity": "canonical-json-sha256",
    "source_commit": "full-lowercase-git-sha1",
    "image_identity": "oci-index-and-platform-manifest-digests",
    "tag_trust": "none-digest-only",
    "platform": "linux/amd64",
    "registry_resolution": "rehash-index-exact-one-linux-amd64-manifest",
    "visibility": "anonymous-public-before-native-qualification",
    "runtime_user": "crossforge:1000:1000",
    "sdk_root": "root-owned-runtime-immutable",
    "writable_roots": ["home", "cache", "tmp", "workspace"],
    "native_aarch64_release": {
        "runner_label": "ubuntu-24.04-arm",
        "runner_arch": "ARM64",
        "runtime": "pinned-rocky-8.10-arm64-manifest",
        "candidate_input": "exact-public-oci-digest",
        "compiler_input": "candidate-contained-aarch64-cross-toolchain",
        "transfer": "deterministic-tar-sha256",
        "execution": "native-no-qemu",
        "probe_bundle_schema": (
            "https://crossforge.dev/schemas/"
            "native-aarch64-probe-bundle.schema.json"
        ),
        "qualification_schema": (
            "https://crossforge.dev/schemas/"
            "native-aarch64-qualification.schema.json"
        ),
        "artifacts": [
            "catch",
            "hello",
            "libgcc-helper",
            "libstdc++-nonshared-audit.so",
            "libthrow.so",
            "lto",
            "lto-archive",
            "modern",
        ],
    },
}
