"""SDK retries retain selections; registry and execution boundaries are fixtures."""

import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import test_component_recovery as raw_fixtures
import test_python_sdk_catalog as fixtures

from crossforge_internal import component_recovery as raw, python_sdk_recovery as recovery
from crossforge_internal.identity import IdentityError, content_sha256, load_json

ROOT, catalog, CLI = fixtures.ROOT, fixtures.catalog, fixtures.CLI
RECOVERY_CONTEXT = recovery.context


def row_selection(row):
    value = raw_fixtures.selection("qualification/python-" + row, "qualification")
    value["status"] = "verified-qualified-row"
    return value


class SdkRecoveryDocumentTests(unittest.TestCase):
    def setUp(self):
        self.current = {"stage": "sdk-assembly", "targets": ["python-dev"],
                        "source_commit": "a" * 40, "source_inventory_sha256": "b" * 64}
        self.expected = {"x86_64-toolchain-install": {"component": "toolchain/x86_64", "role": "toolchain-install"}}
        self.raw = {name: raw_fixtures.selection(**identity) for name, identity in self.expected.items()}
        self.rows = {"cp39": row_selection("cp39")}
        self.value = recovery.document(self.current, self.raw, self.rows, self.expected, ["cp39"])

    def test_document_preserves_exact_producer_and_references_without_local_paths(self):
        pins = recovery.verify(self.value, content_sha256(self.value), self.current, self.expected, ["cp39"])
        self.assertEqual(pins["rows"]["cp39"]["producer"], self.rows["cp39"]["producer"])
        self.assertNotIn("/old/", json.dumps(self.value))
        self.assertEqual(self.value["raw"]["schema_version"], 1)
        with self.assertRaises(IdentityError):
            raw.validate_pin(pins["rows"]["cp39"])
        pins["rows"]["cp39"]["producer"]["source_commit"] = "0" * 40
        self.assertEqual(self.value["rows"]["cp39"]["producer"]["source_commit"], "e" * 40)

    def test_schema_rows_roles_registry_and_producer_are_strict(self):
        for change in ("schema", "extra", "missing-row", "extra-row", "wrong-row", "raw-role", "tag", "registry", "local", "dirty"):
            value = copy.deepcopy(self.value)
            pin = value["rows"]["cp39"]
            if change == "schema":
                value["schema_version"] = True
            elif change == "extra":
                value["extra"] = 1
            elif change == "missing-row":
                value["rows"].clear()
            elif change == "extra-row":
                value["rows"]["cp310"] = row_selection("cp310")
            elif change == "wrong-row":
                pin["component"] = "qualification/python-cp310"
            elif change == "raw-role":
                pin["role"] = "python-install"
            elif change == "tag":
                pin["catalog_reference"] = raw.REPOSITORY + ":latest"
            elif change == "registry":
                pin["reference"] = "ghcr.io/other/component@sha256:" + "0" * 64
            elif change == "local":
                pin["producer"].update(kind="local", invocation="urn:crossforge:local:fixture")
            else:
                pin["producer"]["source_dirty"] = True
            with self.subTest(change=change), self.assertRaises(IdentityError):
                recovery.validate(value, self.expected, ["cp39"])

    def test_independent_digest_and_source_root_environment_inventory_must_match(self):
        changed = copy.deepcopy(self.value)
        changed["rows"]["cp39"]["inputs_sha256"] = "0" * 64
        with self.assertRaisesRegex(IdentityError, "selected SHA256"):
            recovery.verify(changed, content_sha256(self.value), self.current, self.expected, ["cp39"])
        for field, value in (("source_commit", "c" * 40), ("targets", ["sdk-complete-dev"]),
                             ("source_inventory_sha256", "d" * 64)):
            with self.subTest(field=field), self.assertRaisesRegex(IdentityError, "recovery source"):
                recovery.verify(self.value, content_sha256(self.value), dict(self.current, **{field: value}), self.expected, ["cp39"])

    def test_row_selection_cannot_replace_any_pinned_identity_but_paths_may_move(self):
        pin = self.value["rows"]["cp39"]
        for field in raw.PIN_FIELDS:
            result = copy.deepcopy(self.rows["cp39"])
            if field == "producer":
                result[field]["invocation"] = "https://github.com/eglinuxer/crossforge/actions/runs/124/attempts/1"
            elif field == "catalog_reference":
                result["catalog"]["reference"] = raw.REPOSITORY + "@sha256:" + "0" * 64
            elif field == "receipt_sha256":
                result["subject"][field] = "0" * 64
            else:
                result[field] = raw.REPOSITORY + "@sha256:" + "0" * 64 if field == "reference" else "0" * 64
            with self.subTest(field=field), self.assertRaises(IdentityError):
                recovery.verify_row_selection(pin, result, "cp39")
        moved = copy.deepcopy(self.rows["cp39"])
        moved["subject"].update(receipt="/new/receipt.json", layout="/new/oci")
        self.assertEqual(recovery.verify_row_selection(pin, moved, "cp39"), moved)
        with self.assertRaises(IdentityError):
            recovery.verify_row_selection(pin, {"status": "qualification-required"}, "cp39")

    def test_revision_requires_a_real_clean_complete_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            def git(*args):
                return subprocess.check_output(["git"] + list(args), cwd=str(source), stderr=subprocess.PIPE)
            git("init")
            (source / "input").write_text("original\n")
            git("add", "input")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Fixture")
            self.assertEqual(recovery.revision(source), git("rev-parse", "HEAD").decode().strip())
            (source / "input").write_text("changed\n")
            with self.assertRaisesRegex(IdentityError, "clean source"):
                recovery.revision(source)


class SdkRecoveryAcquisitionTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.PythonSdkCatalogTests.setUpClass.__func__)
    patch = fixtures.PythonSdkCatalogTests.patch

    def setUp(self):
        fixtures.PythonSdkCatalogTests.setUp(self)
        self.current = {"stage": "sdk-assembly", "targets": ["python-dev"],
                        "source_commit": "a" * 40, "source_inventory_sha256": "b" * 64}
        self.expected = raw.requirements(ROOT, self.graph, True)
        for group in (self.toolchains, self.python):
            for name, result in group["components"].items():
                subject = result["subject"]
                subject.setdefault("receipt", "/fixture/" + name + "/receipt.json")
                subject.setdefault("receipt_sha256", "d" * 64)
                subject.setdefault("layout", "/fixture/" + name + "/oci")
                result.update(raw_fixtures.selection(**self.expected[name]), subject=subject)
        self.qualified = {row: dict(row_selection(row), subject=value["qualification"])
                          for row, value in self.components["rows"].items()}
        self.row_resolver.side_effect = lambda *args, **kwargs: self.qualified[args[2]]
        self.context = self.patch(recovery, "context", return_value=self.current)

    def acquire(self, **options):
        return catalog.acquire(ROOT, self.graph, "python-dev", self.execution, self.data, self.evidence,
            "builder", self.root / "oras", self.root / "cosign", record_recovery=True, **options)

    def saved(self):
        return recovery.document(self.current, dict(self.toolchains["components"], **self.python["components"]),
                                 self.qualified, self.expected, sorted(self.qualified))

    def selected(self):
        value = self.saved()
        return {"document": value, "sha256": content_sha256(value)}

    def test_ready_checkpoint_records_all_32_raw_and_six_qualified_selections(self):
        result = self.acquire()
        value = load_json(result["component_recovery"]["path"])
        self.assertEqual(value, self.saved())
        self.assertEqual(len(value["raw"]["components"]), 32)
        self.assertEqual(len(value["rows"]), 6)
        self.assertEqual(content_sha256(value), result["component_recovery"]["sha256"])
        self.assertEqual(result["components"], self.components)

    def test_retry_forwards_exact_catalogs_and_preserves_checkpoint_digest(self):
        selected = self.selected()
        result = self.acquire(recovery=selected)
        self.assertEqual(result["component_recovery"]["sha256"], selected["sha256"])
        for resolver, group in ((self.toolchain_resolver, self.toolchains), (self.python_resolver, self.python)):
            self.assertEqual(resolver.call_args[1]["recovery"],
                {name: selected["document"]["raw"]["components"][name] for name in group["components"]})
        for call in self.row_resolver.call_args_list:
            row = call[0][2]
            self.assertEqual(call[1], {"catalog_reference": selected["document"]["rows"][row]["catalog_reference"]})

    def test_invalid_or_wrong_source_checkpoint_fails_before_registry_access(self):
        selected = self.selected()
        selected["sha256"] = "0" * 64
        with self.assertRaisesRegex(IdentityError, "selected SHA256"):
            self.acquire(recovery=selected)
        selected = self.selected()
        self.context.return_value = dict(self.current, source_commit="f" * 40)
        with self.assertRaisesRegex(IdentityError, "recovery source"):
            self.acquire(recovery=selected)
        self.toolchain_resolver.assert_not_called()
        self.row_resolver.assert_not_called()

    def test_fixed_missing_or_replaced_row_is_fatal_without_new_checkpoint(self):
        selected = self.selected()
        for changed in ({"status": "qualification-required"}, dict(self.qualified["cp39"], inputs_sha256="0" * 64)):
            self.qualified["cp39"] = changed
            with self.assertRaises(IdentityError):
                self.acquire(recovery=selected)
            self.assertFalse((self.evidence / "component-recovery.json").exists())
            self.assertFalse((self.evidence / "components.json").exists())

    def test_partial_acquisition_does_not_create_a_recovery_document(self):
        self.qualified["cp39"] = {"status": "qualification-required"}
        result = self.acquire()
        self.assertEqual(result["status"], "components-required")
        self.assertIsNone(result["component_recovery"])
        self.assertFalse((self.evidence / "component-recovery.json").exists())

    def test_source_change_during_acquisition_prevents_ready_checkpoint(self):
        self.context.side_effect = [self.current, dict(self.current, source_inventory_sha256="0" * 64)]
        with self.assertRaisesRegex(IdentityError, "inputs changed"):
            self.acquire()
        self.assertFalse((self.evidence / "component-recovery.json").exists())
        self.assertFalse((self.evidence / "components.json").exists())

    def test_failed_integration_retains_checkpoint_for_same_selection_retry(self):
        def fail(*args):
            self.assertEqual(load_json(self.evidence / "acquisition/component-recovery.json"), self.saved())
            raise IdentityError("fixture failed final integration")
        with mock.patch.object(fixtures.python_sdk, "execute", side_effect=fail), self.assertRaises(IdentityError):
            catalog.execute(ROOT, self.graph, "python-dev", self.execution, self.data, self.evidence,
                "builder", self.root / "oras", self.root / "cosign", record_recovery=True)
        value = load_json(self.evidence / "acquisition/component-recovery.json")
        self.assertFalse((self.evidence / "result.json").exists())
        self.data, self.evidence = self.root / "retry-data", self.root / "retry-evidence"
        with mock.patch.object(fixtures.python_sdk, "execute", return_value={"fixture": "fresh final integration"}) as execute:
            result = catalog.execute(ROOT, self.graph, "python-dev", self.execution, self.data, self.evidence,
                "builder", self.root / "oras", self.root / "cosign",
                recovery={"document": value, "sha256": content_sha256(value)})
        execute.assert_called_once()
        self.assertEqual(result["acquisition"]["component_recovery"]["sha256"], content_sha256(value))

    def test_post_integration_source_drift_prevents_success(self):
        def drift(*args):
            self.context.return_value = dict(self.current, source_commit="f" * 40)
            return {"fixture": "integration"}
        with mock.patch.object(fixtures.python_sdk, "execute", side_effect=drift), self.assertRaisesRegex(IdentityError, "recovery source"):
            catalog.execute(ROOT, self.graph, "python-dev", self.execution, self.data, self.evidence,
                "builder", self.root / "oras", self.root / "cosign", record_recovery=True)
        self.assertFalse((self.evidence / "result.json").exists())

    def test_post_integration_environment_drift_prevents_success(self):
        def drift(*args):
            self.environment.return_value = dict(self.execution, host={"fixture": "different CPU"})
            return {"fixture": "integration"}
        with mock.patch.object(fixtures.python_sdk, "execute", side_effect=drift), self.assertRaisesRegex(IdentityError, "environment changed"):
            catalog.execute(ROOT, self.graph, "python-dev", self.execution, self.data, self.evidence,
                "builder", self.root / "oras", self.root / "cosign", record_recovery=True)
        self.assertFalse((self.evidence / "result.json").exists())

    def test_real_source_context_binds_reachable_recipe_and_full_physical_environment(self):
        with mock.patch.object(recovery, "revision", return_value="a" * 40):
            current = RECOVERY_CONTEXT(ROOT, self.graph, "python-dev", self.execution)
            changed = RECOVERY_CONTEXT(ROOT, self.graph, "python-dev", dict(self.execution, host={"fixture": "different CPU"}))
        self.assertNotEqual(current["source_inventory_sha256"], changed["source_inventory_sha256"])
        self.assertEqual(current["source_commit"], "a" * 40)


class SdkRecoveryCliTests(unittest.TestCase):
    setUp = fixtures.PythonSdkCatalogCliTests.setUp
    args = fixtures.PythonSdkCatalogCliTests.args

    def test_cli_passes_record_or_independently_pinned_recovery_options(self):
        path = self.root / "recovery.json"
        path.write_text('{"fixture": "checkpoint"}')
        for command, operation in (("acquire-python-sdk", "acquire"), ("execute-python-sdk-catalog", "execute")):
            with mock.patch.object(catalog, operation, return_value={"status": "ready"}) as call, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(CLI["main"](self.args(command) + ["--record-component-recovery"]), 0)
                self.assertEqual(call.call_args[1], {"record_recovery": True})
                self.assertEqual(CLI["main"](self.args(command) + ["--component-recovery", str(path),
                    "--component-recovery-sha256", "a" * 64]), 0)
                self.assertEqual(call.call_args[1], {"record_recovery": False,
                    "recovery": {"document": {"fixture": "checkpoint"}, "sha256": "a" * 64}})

    def test_cli_rejects_unpaired_or_unsafe_recovery_files_before_consumption(self):
        path = self.root / "recovery.json"
        path.symlink_to(self.root / "execution.json")
        for args in (["--component-recovery", str(path)], ["--component-recovery-sha256", "a" * 64],
                     ["--component-recovery", str(path), "--component-recovery-sha256", "a" * 64]):
            with mock.patch.object(catalog, "acquire") as acquire, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(CLI["main"](self.args("acquire-python-sdk") + args), 1)
                acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
