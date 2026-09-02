import copy
import hashlib
import json
import runpy
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
VALIDATOR = runpy.run_path(str(REPOSITORY / "scripts/validate-release.py"))
EVIDENCE_VALIDATOR = runpy.run_path(
    str(REPOSITORY / "scripts/validate-supply-chain-evidence.py")
)
SOURCE_FETCHER = runpy.run_path(str(REPOSITORY / "scripts/fetch-release-source.py"))


class ReleaseValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = VALIDATOR["load_json"](REPOSITORY / "config/release.json")
        cls.schema = VALIDATOR["load_json"](
            REPOSITORY / "config/schemas/release.schema.json"
        )

    def test_current_configuration_is_valid(self):
        VALIDATOR["validate_schema_subset"](self.schema)
        VALIDATOR["validate"](self.config, self.schema, self.schema, "$")

    def test_supply_chain_evidence_is_bound_to_configuration(self):
        result = EVIDENCE_VALIDATOR["validate_evidence"](self.config, REPOSITORY)
        self.assertEqual(
            result["rocky_index_sha256"],
            self.config["base_image"]["digest"],
        )
        self.assertEqual(
            result["qemu_manifest_sha256"],
            self.config["qemu"]["executor"]["manifest_digest"],
        )
        self.assertEqual(result["python_patches"], 4)

    def test_supply_chain_identity_tampering_is_rejected(self):
        mutations = (
            (("base_image", "manifests", "arm64"), "sha256:" + "0" * 64),
            (("qemu", "executor", "provenance", "builder_commit"), "0" * 40),
            (("qemu", "executor", "source", "commit"), "0" * 40),
            (("vcpkg", "release", "commit"), "0" * 40),
            (("vcpkg", "tool", "sha256"), "0" * 64),
            (
                ("vcpkg", "tool", "signature", "key", "fingerprint"),
                "0" * 40,
            ),
            (
                ("vcpkg", "tool", "license", "notice_sha256"),
                "0" * 64,
            ),
            (("python", "versions", 4, "source", "sha256"), "0" * 64),
            (
                ("python", "versions", 4, "source", "sigstore", "bundle_sha256"),
                "0" * 64,
            ),
            (("python", "versions", 0, "patches", 0, "sha256"), "0" * 64),
            (("python", "versions", 1, "patches", 0, "sha256"), "0" * 64),
            (("python", "versions", 2, "patches", 0, "sha256"), "0" * 64),
            (("python", "versions", 3, "patches", 0, "sha256"), "0" * 64),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                config = copy.deepcopy(self.config)
                parent = config
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = value
                with self.assertRaises(EVIDENCE_VALIDATOR["EvidenceError"]):
                    EVIDENCE_VALIDATOR["validate_evidence"](config, REPOSITORY)

    def test_duplicate_keys_are_rejected(self):
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["reject_duplicate_keys"]([("same", 1), ("same", 2)])

    def test_unknown_configuration_fields_are_rejected(self):
        config = copy.deepcopy(self.config)
        config["unexpected"] = True
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate"](config, self.schema, self.schema, "$")

    def test_abi_identity_schema_rejects_swaps_and_unknown_fields(self):
        swapped = copy.deepcopy(self.config)
        swapped["abi"]["targets"]["x86_64"]["baseline"][
            "file"
        ] = "abi/el8/aarch64.json"
        extra = copy.deepcopy(self.config)
        extra["abi"]["python"]["unknown"] = {}
        for config in (swapped, extra):
            with self.subTest(config=config["abi"]):
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["validate"](
                        config, self.schema, self.schema, "$"
                    )

    def test_unsupported_schema_keywords_are_rejected(self):
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate_schema_subset"](
                {"type": "object", "dependentRequired": {}}
            )

    def test_pending_sources_are_explicit(self):
        self.assertTrue(VALIDATOR["find_pending"](self.config))

    def test_locked_sysroot_digests_match_files(self):
        locked = 0
        for target in self.config["targets"]:
            pin = target["sysroot"]
            if pin["status"] != "locked":
                continue
            locked += 1
            lock = VALIDATOR["load_json"](REPOSITORY / pin["lock_file"])
            canonical = json.dumps(
                lock, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            self.assertEqual(pin["canonical_sha256"], digest)
        self.assertEqual(locked, 2)

    def test_release_abi_identity_digests_match_files(self):
        abi = self.config["abi"]
        identities = [abi["provider_manifest"]]
        for arch in ("x86_64", "aarch64"):
            identities.extend(
                (
                    abi["targets"][arch]["baseline"],
                    abi["targets"][arch]["sysroot_inventory"],
                    abi["python"]["provider_catalogs"][arch],
                )
            )
        identities.append(abi["python"]["runtime_provider_policy"])
        self.assertEqual(len(identities), 8)
        for identity in identities:
            with self.subTest(file=identity["file"]):
                document = VALIDATOR["load_json"](
                    REPOSITORY / identity["file"]
                )
                canonical = json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.assertEqual(
                    identity["canonical_sha256"],
                    hashlib.sha256(canonical).hexdigest(),
                )

    def test_qemu_executor_identity_is_coherent(self):
        qemu = self.config["qemu"]
        executor = qemu["executor"]
        self.assertEqual(executor["status"], "locked")
        self.assertIn(qemu["version"], executor["tag"])
        self.assertEqual(executor["source"]["tag"], "v" + qemu["version"])
        self.assertNotEqual(
            executor["provenance"]["builder_commit"],
            executor["source"]["commit"],
        )
        self.assertRegex(
            executor["provenance"]["attestation_manifest_digest"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertRegex(executor["binary_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(executor["cpu"], "cortex-a53")

    def test_locked_rpm_sources_have_size_and_spec_hash(self):
        for component in (self.config["gts"], self.config["binutils"]):
            source = component["source"]
            self.assertEqual(source["status"], "locked")
            self.assertGreater(source["size"], 0)
            self.assertRegex(source["spec_sha256"], r"^[0-9a-f]{64}$")

    def test_zstd_release_supply_chain_is_exact(self):
        zstd = self.config["python"]["zstd"]
        source = zstd["source"]
        signature = source["signature"]
        git = source["git"]
        self.assertEqual(zstd["version"], "1.5.7")
        self.assertEqual(source["status"], "locked")
        self.assertEqual(source["size"], 2434947)
        self.assertEqual(
            source["sha256"],
            "eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3",
        )
        self.assertEqual(signature["size"], 858)
        self.assertEqual(
            signature["key"]["fingerprint"],
            "4ef4ac63455fc9f4545d9b7def8fe99528b52ffd",
        )
        self.assertEqual(
            git["tag_object"], "ac66b19e6bd6b83238bf008eecc1298105298532"
        )
        self.assertEqual(
            git["commit"], "f8745da6ff1ad1e7bab384bd1f9d742439278e99"
        )
        self.assertEqual(zstd["license"]["expression"], "BSD-3-Clause")
        evidence = EVIDENCE_VALIDATOR["validate_evidence"](
            self.config, REPOSITORY
        )
        self.assertEqual(evidence["zstd_tag_object"], git["tag_object"])
        self.assertEqual(evidence["zstd_commit"], git["commit"])

    def test_zstd_evidence_and_license_tampering_is_rejected(self):
        mutations = (
            (("source", "signature", "sha256"), "0" * 64),
            (("source", "signature", "key", "fingerprint"), "0" * 40),
            (("source", "git", "tag_object"), "0" * 40),
            (("source", "git", "commit"), "0" * 40),
            (("license", "expression"), "GPL-2.0-only"),
            (("license", "license_sha256"), "0" * 64),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                config = copy.deepcopy(self.config)
                parent = config["python"]["zstd"]
                for part in path[:-1]:
                    parent = parent[part]
                parent[path[-1]] = value
                with self.assertRaises(EVIDENCE_VALIDATOR["EvidenceError"]):
                    EVIDENCE_VALIDATOR["validate_evidence"](config, REPOSITORY)

    def test_vcpkg_registry_tool_signature_and_licenses_are_exact(self):
        vcpkg = self.config["vcpkg"]
        release = vcpkg["release"]
        tool = vcpkg["tool"]
        self.assertEqual(release["tag"], "2026.07.29")
        self.assertEqual(
            release["commit"],
            "9e593bb18ea69cc5095e012465dcd675a822ed0d",
        )
        self.assertEqual(tool["tag"], "2026-07-27")
        self.assertEqual(
            tool["commit"],
            "98d7cb0cf1f4686a3e43aa5672b6230c1d56bce8",
        )
        self.assertEqual(tool["size"], 8548168)
        self.assertEqual(
            tool["signature"]["key"]["fingerprint"],
            "bc528686b50d79e339d3721ceb3e94adbe1229cf",
        )
        evidence = EVIDENCE_VALIDATOR["validate_evidence"](
            self.config, REPOSITORY
        )
        self.assertEqual(evidence["vcpkg_commit"], release["commit"])
        self.assertEqual(evidence["vcpkg_tool_commit"], tool["commit"])

    def test_vcpkg_schema_rejects_incomplete_or_unlocked_tool(self):
        for mutate in (
            lambda value: value["vcpkg"]["tool"].pop("sha512"),
            lambda value: value["vcpkg"]["tool"].__setitem__(
                "status", "pending"
            ),
            lambda value: value["vcpkg"]["release"].__setitem__(
                "commit", None
            ),
        ):
            config = copy.deepcopy(self.config)
            mutate(config)
            with self.assertRaises(VALIDATOR["ValidationError"]):
                VALIDATOR["validate"](
                    config, self.schema, self.schema, "$"
                )

    def test_rocky_trust_root_matches_key_file(self):
        trust = self.config["trust"]["rocky_rpm_key"]
        digest = hashlib.sha256((REPOSITORY / trust["file"]).read_bytes()).hexdigest()
        self.assertEqual(trust["sha256"], digest)

    def test_nonpositive_locked_source_size_is_rejected(self):
        config = copy.deepcopy(self.config)
        config["gts"]["source"]["size"] = 0
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate"](config, self.schema, self.schema, "$")

    def test_python_sigstore_evidence_cannot_claim_unimplemented_verification(self):
        config = copy.deepcopy(self.config)
        config["python"]["versions"][4]["source"]["sigstore"][
            "verification"
        ] = "verified"
        with self.assertRaises(VALIDATOR["ValidationError"]):
            VALIDATOR["validate"](config, self.schema, self.schema, "$")

    def test_python_39_source_and_sigstore_evidence_are_exactly_locked(self):
        entry = self.config["python"]["versions"][0]
        self.assertEqual(entry["version"], "3.9.25")
        self.assertEqual(entry["adapter"], "legacy")
        self.assertEqual(entry["support"], "eol")
        self.assertEqual(
            entry["source"],
            {
                "status": "locked",
                "url": "https://www.python.org/ftp/python/3.9.25/Python-3.9.25.tar.xz",
                "sha256": "00e07d7c0f2f0cc002432d1ee84d2a40dae404a99303e3f97701c10966c91834",
                "size": 20183236,
                "sigstore": {
                    "verification": "archived-unverified",
                    "bundle_url": "https://www.python.org/ftp/python/3.9.25/Python-3.9.25.tar.xz.sigstore",
                    "bundle_sha256": "09243be5b795dbccf2e3a28b1a478a51ccdc1d2e102122737fc012798a5b17ad",
                    "bundle_size": 5079,
                    "bundle_evidence": "evidence/sigstore/cpython-3.9.25.tar.xz.sigstore.b64",
                    "identity": "lukasz@langa.pl",
                    "oidc_issuer": "https://github.com/login/oauth",
                },
            },
        )
        self.assertEqual(
            SOURCE_FETCHER["source_for"](self.config, "python", "3.9.25"),
            entry["source"],
        )
        evidence = EVIDENCE_VALIDATOR["validate_evidence"](
            self.config, REPOSITORY
        )
        self.assertEqual(evidence["python_sources"], 6)
        self.assertEqual(evidence["python_sigstore_status"], "archived-unverified")

    def test_python_isolation_patches_are_explicit_and_content_locked(self):
        versions = self.config["python"]["versions"]
        expected = {
            0: {
                "version": "3.9.25",
                "adapter": "legacy",
                "patch": {
                    "file": "patches/cpython/3.9/0001-gh-115382-isolate-target-sysconfig.patch",
                    "sha256": "e4d5629748d9737c891f47eb38cb3a5722c3b71afc5e28b5cede80ae5b66cf77",
                },
            },
            1: {
                "version": "3.10.21",
                "adapter": "legacy",
                "patch": {
                    "file": "patches/cpython/3.10/0001-gh-115382-isolate-target-sysconfig.patch",
                    "sha256": "af23410fcaef3bb630dc0b986b5de52a542f3e1945c2493261a92500357773d3",
                },
            },
            2: {
                "version": "3.11.16",
                "adapter": "transition",
                "patch": {
                    "file": "patches/cpython/3.11/0001-gh-115382-isolate-target-sysconfig.patch",
                    "sha256": "072dacfcc57b06bc1e5382726990627593a36e1f08232cb790db42ae334a49aa",
                },
            },
            3: {
                "version": "3.12.14",
                "adapter": "modern",
                "patch": {
                    "file": "patches/cpython/3.12/0001-gh-115382-isolate-target-sysconfig.patch",
                    "sha256": "ff3a8e2695b4c66d0f60e6c73ac0028221ef803a308ff4e81393a54c9404dd33",
                },
            },
        }
        for index, contract in expected.items():
            with self.subTest(index=index):
                version = versions[index]
                self.assertEqual(version["version"], contract["version"])
                self.assertEqual(version["adapter"], contract["adapter"])
                self.assertEqual(version["patches"], [contract["patch"]])
                patch = version["patches"][0]
                payload = (REPOSITORY / patch["file"]).read_bytes()
                self.assertEqual(
                    hashlib.sha256(payload).hexdigest(), patch["sha256"]
                )
                self.assertEqual(
                    [
                        line
                        for line in payload.splitlines()
                        if line.startswith(b"diff --git ")
                    ],
                    (
                        [
                            b"diff --git a/Lib/distutils/sysconfig.py b/Lib/distutils/sysconfig.py",
                            b"diff --git a/Lib/sysconfig.py b/Lib/sysconfig.py",
                            b"diff --git a/configure b/configure",
                            b"diff --git a/configure.ac b/configure.ac",
                        ]
                        if contract["version"] == "3.9.25"
                        else [
                            b"diff --git a/Lib/sysconfig.py b/Lib/sysconfig.py",
                            b"diff --git a/configure b/configure",
                            b"diff --git a/configure.ac b/configure.ac",
                        ]
                    ),
                )
                self.assertIn(b"_PYTHON_SYSCONFIGDATA_PATH", payload)
                self.assertIn(b"PYTHONPATH=$(srcdir)/Lib", payload)
                if contract["version"] == "3.9.25":
                    self.assertIn(
                        b"from sysconfig import _init_posix as sysconfig_init_posix",
                        payload,
                    )
                    self.assertIn(
                        b"sysconfig_init_posix(config_vars)", payload
                    )
        for index, version in enumerate(versions):
            if index not in expected:
                self.assertEqual(version["patches"], [])

    def test_python_isolation_patch_schema_rejects_contract_drift(self):
        mutations = []

        for index, minor in (
            (0, "3.9"),
            (1, "3.10"),
            (2, "3.11"),
            (3, "3.12"),
        ):
            missing = copy.deepcopy(self.config)
            missing["python"]["versions"][index]["patches"] = []
            mutations.append(missing)

            wrong_path = copy.deepcopy(self.config)
            wrong_path["python"]["versions"][index]["patches"][0]["file"] = (
                "patches/cpython/%s/0001-other.patch" % minor
            )
            mutations.append(wrong_path)

        unexpected = copy.deepcopy(self.config)
        unexpected["python"]["versions"][4]["patches"] = copy.deepcopy(
            unexpected["python"]["versions"][2]["patches"]
        )
        mutations.append(unexpected)

        for index, config in enumerate(mutations):
            with self.subTest(index=index):
                with self.assertRaises(VALIDATOR["ValidationError"]):
                    VALIDATOR["validate"](config, self.schema, self.schema, "$")

    def test_python_patch_loader_rejects_noncanonical_path(self):
        with self.assertRaises(EVIDENCE_VALIDATOR["EvidenceError"]):
            EVIDENCE_VALIDATOR["load_cpython_patch"](
                REPOSITORY,
                "patches/cpython/../cpython/3.11/"
                "0001-gh-115382-isolate-target-sysconfig.patch",
            )

    def test_python_isolation_policy_fails_closed_for_unknown_patch_release(self):
        for index, replacement in (
            (0, "3.9.26"),
            (1, "3.10.22"),
            (2, "3.11.17"),
            (3, "3.12.15"),
        ):
            with self.subTest(version=replacement):
                config = copy.deepcopy(self.config)
                entry = config["python"]["versions"][index]
                original = entry["version"]
                entry["version"] = replacement
                source = entry["source"]
                source["url"] = source["url"].replace(original, replacement)
                source["sigstore"]["bundle_url"] = source["sigstore"][
                    "bundle_url"
                ].replace(original, replacement)
                entry["patches"] = []
                with self.assertRaisesRegex(
                    EVIDENCE_VALIDATOR["EvidenceError"],
                    "no audited isolation patch policy",
                ):
                    EVIDENCE_VALIDATOR["validate_evidence"](config, REPOSITORY)

    def test_python_313_supply_policy_rejects_injected_patch(self):
        config = copy.deepcopy(self.config)
        config["python"]["versions"][4]["patches"] = copy.deepcopy(
            config["python"]["versions"][3]["patches"]
        )
        with self.assertRaisesRegex(
            EVIDENCE_VALIDATOR["EvidenceError"], "unexpected CPython patch"
        ):
            EVIDENCE_VALIDATOR["validate_evidence"](config, REPOSITORY)

    def test_python_source_selection_requires_one_exact_version(self):
        with self.assertRaises(SOURCE_FETCHER["ValidationError"]):
            SOURCE_FETCHER["source_for"](self.config, "python", None)
        config = copy.deepcopy(self.config)
        config["python"]["versions"].append(
            copy.deepcopy(config["python"]["versions"][4])
        )
        with self.assertRaises(SOURCE_FETCHER["ValidationError"]):
            SOURCE_FETCHER["source_for"](config, "python", "3.13.15")

    def test_bake_override_is_current(self):
        renderer = runpy.run_path(str(REPOSITORY / "scripts/render-bake.py"))
        expected = renderer["render"](REPOSITORY)
        actual = (REPOSITORY / "docker-bake.override.json").read_text(encoding="utf-8")
        self.assertEqual(actual, expected)

    def test_bake_contexts_consume_platform_manifest_pins(self):
        override = json.loads(
            (REPOSITORY / "docker-bake.override.json").read_text(encoding="utf-8")
        )
        contexts = override["target"]["_common"]["contexts"]
        base = self.config["base_image"]
        executor = self.config["qemu"]["executor"]
        self.assertTrue(
            contexts["crossforge_rocky_amd64"].endswith(
                base["manifests"]["amd64"]
            )
        )
        self.assertTrue(
            contexts["crossforge_rocky_arm64"].endswith(
                base["manifests"]["arm64"]
            )
        )
        self.assertNotIn("crossforge_qemu", contexts)
        for name in (
            "qemu-aarch64-validated",
            "runtime-smoke-aarch64",
            "toolchain-aarch64-dev",
        ):
            self.assertTrue(
                override["target"][name]["contexts"]["crossforge_qemu"].endswith(
                    executor["manifest_digest"]
                )
            )


if __name__ == "__main__":
    unittest.main()
