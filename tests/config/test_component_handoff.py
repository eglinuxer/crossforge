import copy
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_artifacts, component_build, component_handoff as handoff, component_inputs
    from crossforge_internal.identity import IdentityError, content_sha256
    PILOT = runpy.run_path(str(ROOT / "scripts/component-pilot.py"))
finally:
    sys.path.pop(0)


class ComponentHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "source").write_text("fixture")
        (self.root / "component").mkdir()
        self.producer = {"kind": "github-actions", "source_commit": "a" * 40, "source_dirty": False,
                         "invocation": "https://github.com/eglinuxer/crossforge/actions/runs/123/attempts/2",
                         "started_at": "2026-09-10T00:00:00Z"}
        self.execution = {"buildkit_image": "image@sha256:" + "b" * 64}
        self.components = {}
        for index, role in enumerate(handoff.ROLES):
            spec = component_build.toolchain_spec("x86_64", role)
            inputs = component_inputs.capture(self.root, spec["component"], "build", ["source"], [spec["triple"]],
                parameters={"execution": self.execution})
            contract = component_artifacts.contract(role, inputs, self.producer)
            (self.root / component_artifacts.CONTRACT_PATH).write_text(json.dumps(contract))
            digest = "sha256:" + str(index + 1) * 64
            observation = {"kind": "crossforge-oci-layout-observation", "schema_version": 1, "platform": "linux/amd64",
                           "root_digest": digest, "platform_digest": digest, "config_digest": "sha256:" + "c" * 64}
            receipt = component_artifacts.receipt(contract, observation, self.root, [component_artifacts.CONTRACT_PATH])
            self.components[role] = {"reference": handoff.REPOSITORY + "@" + digest,
                                      "receipt_sha256": content_sha256(receipt), "receipt": receipt}
        self.value = handoff.document(self.producer, self.execution, self.components)

    def verify(self, value=None, digest=None, commit=None, invocation=None):
        return handoff.verify(value or self.value, digest or content_sha256(self.value),
            commit or self.producer["source_commit"], invocation or self.producer["invocation"])

    def test_same_run_exact_handoff_is_accepted_with_original_producer(self):
        self.assertEqual(self.verify()["producer"], self.producer)

    def test_partial_main_handoff_requires_explicit_architecture_and_keeps_original_receipts(self):
        for role in handoff.ROLES:
            value = handoff.document(self.producer, self.execution, {role: self.components[role]}, "x86_64")
            verified = self.verify(value, content_sha256(value))
            self.assertEqual(verified["components"][role], self.components[role])
            self.assertEqual(verified["schema_version"], 2)
            with self.assertRaises(IdentityError):
                handoff.document(self.producer, self.execution, {role: self.components[role]})

    def test_partial_handoff_cannot_omit_all_roles_or_mix_architecture(self):
        for arch, components in (("aarch64", self.components), ("unknown", self.components), ("x86_64", {})):
            with self.subTest(arch=arch, roles=list(components)), self.assertRaises(IdentityError):
                handoff.document(self.producer, self.execution, components, arch)
        value = handoff.document(self.producer, self.execution, self.components, "x86_64")
        del value["architecture"]
        with self.assertRaises(IdentityError):
            handoff.validate(value)

    def test_upstream_job_digest_commit_and_attempt_are_independent_requirements(self):
        for kwargs in ({"digest": "0" * 64}, {"commit": "b" * 40},
                       {"invocation": "https://github.com/eglinuxer/crossforge/actions/runs/123/attempts/3"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(IdentityError):
                self.verify(**kwargs)

    def test_missing_unknown_or_mixed_component_handoff_is_rejected(self):
        for change in ("unknown", "schema", "missing-role", "wrong-role", "wrong-receipt", "wrong-registry", "wrong-environment", "mixed-producer"):
            value = copy.deepcopy(self.value)
            item = value["components"]["toolchain-install"]
            if change == "unknown":
                value["unknown"] = True
            elif change == "schema":
                value["schema_version"] = True
            elif change == "missing-role":
                del value["components"]["gcc-test-context"]
            elif change == "wrong-role":
                value["components"]["toolchain-install"] = value["components"]["gcc-test-context"]
            elif change == "wrong-receipt":
                item["receipt_sha256"] = "0" * 64
            elif change == "wrong-registry":
                item["reference"] = item["reference"].replace("crossforge-components", "other-components")
            elif change == "wrong-environment":
                value["build_execution"] = {"buildkit_image": "different"}
            else:
                item["receipt"]["contract"]["producer"]["source_commit"] = "b" * 40
                item["receipt_sha256"] = content_sha256(item["receipt"])
            with self.subTest(change=change), self.assertRaises(IdentityError):
                self.verify(value, content_sha256(value))

    def test_github_producer_requires_main_dispatch_and_exact_clean_checkout(self):
        environment = {"GITHUB_REPOSITORY": "eglinuxer/crossforge", "GITHUB_SERVER_URL": "https://github.com",
                       "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
                       "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}
        producer = PILOT["github_producer"](environment, "a" * 40, False)
        self.assertEqual(producer["invocation"], self.producer["invocation"])
        for key, value in (("GITHUB_REPOSITORY", "fork/crossforge"), ("GITHUB_REF", "refs/pull/1/merge"),
                           ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_SHA", "b" * 40),
                           ("GITHUB_RUN_ATTEMPT", "invalid"), ("GITHUB_SERVER_URL", "https://other.invalid")):
            with self.subTest(key=key), self.assertRaises(IdentityError):
                PILOT["github_producer"](dict(environment, **{key: value}), "a" * 40, False)
        with self.assertRaises(IdentityError):
            PILOT["github_producer"](environment, "a" * 40, True)


if __name__ == "__main__":
    unittest.main()
