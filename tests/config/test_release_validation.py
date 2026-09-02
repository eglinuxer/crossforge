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
        self.assertEqual(result["python_patches"], 1)

    def test_supply_chain_identity_tampering_is_rejected(self):
        mutations = (
            (("base_image", "manifests", "arm64"), "sha256:" + "0" * 64),
            (("qemu", "executor", "provenance", "builder_commit"), "0" * 40),
            (("qemu", "executor", "source", "commit"), "0" * 40),
            (("python", "versions", 4, "source", "sha256"), "0" * 64),
            (
                ("python", "versions", 4, "source", "sigstore", "bundle_sha256"),
                "0" * 64,
            ),
            (("python", "versions", 2, "patches", 0, "sha256"), "0" * 64),
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

    def test_python_transition_patch_is_explicit_and_content_locked(self):
        versions = self.config["python"]["versions"]
        transition = versions[2]
        self.assertEqual(transition["version"], "3.11.16")
        self.assertEqual(transition["adapter"], "transition")
        self.assertEqual(
            transition["patches"],
            [
                {
                    "file": "patches/cpython/3.11/0001-gh-115382-isolate-target-sysconfig.patch",
                    "sha256": "072dacfcc57b06bc1e5382726990627593a36e1f08232cb790db42ae334a49aa",
                }
            ],
        )
        patch = transition["patches"][0]
        payload = (REPOSITORY / patch["file"]).read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), patch["sha256"])
        self.assertEqual(
            [
                line
                for line in payload.splitlines()
                if line.startswith(b"diff --git ")
            ],
            [
                b"diff --git a/Lib/sysconfig.py b/Lib/sysconfig.py",
                b"diff --git a/configure b/configure",
                b"diff --git a/configure.ac b/configure.ac",
            ],
        )
        self.assertIn(b"_PYTHON_SYSCONFIGDATA_PATH", payload)
        self.assertIn(b"PYTHONPATH=$(srcdir)/Lib", payload)
        for version in versions[:2] + versions[3:]:
            self.assertEqual(version["patches"], [])

    def test_python_transition_patch_schema_rejects_contract_drift(self):
        mutations = []

        missing = copy.deepcopy(self.config)
        del missing["python"]["versions"][2]["patches"]
        mutations.append(missing)

        wrong_path = copy.deepcopy(self.config)
        wrong_path["python"]["versions"][2]["patches"][0]["file"] = (
            "patches/cpython/3.11/0001-other.patch"
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
