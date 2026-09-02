import copy
import contextlib
import hashlib
import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
VERIFY_PATH = REPOSITORY / "docker/verify-python-row.py"
FINALIZE_PATH = REPOSITORY / "docker/finalize-python-row.py"
VERIFY = runpy.run_path(str(VERIFY_PATH))
FINALIZE = runpy.run_path(str(FINALIZE_PATH))
TARGETS = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}


def canonical_sha256(value):
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PythonRowManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        self.release_path = self.directory / "release.json"
        self.write_json(self.release_path, self.release)

    def tearDown(self):
        self.temporary.cleanup()

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def entry(self, minor):
        matches = [
            item
            for item in self.release["python"]["versions"]
            if item["version"].rsplit(".", 1)[0] == minor
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def source_contract(self, minor):
        entry = self.entry(minor)
        row = "cp" + minor.replace(".", "")
        return VERIFY["row_contract"](
            self.release,
            row,
            entry["version"],
            entry["adapter"],
        )

    def run_verify(self, minor, manifest):
        entry = self.entry(minor)
        row = "cp" + minor.replace(".", "")
        manifest_path = self.directory / (row + "-source.json")
        self.write_json(manifest_path, manifest)
        return subprocess.run(
            [
                sys.executable,
                str(VERIFY_PATH),
                "--release",
                str(self.release_path),
                "--row",
                row,
                "--version",
                entry["version"],
                "--adapter",
                entry["adapter"],
                "--manifest",
                str(manifest_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def fixture(self, minor):
        entry = self.entry(minor)
        row = "cp" + minor.replace(".", "")
        root = self.directory / (row + "-root")
        source_manifest = self.source_contract(minor)
        source_path = self.directory / (row + "-prepared-source.json")
        self.write_json(source_path, source_manifest)

        build_bytes = ("build-python-" + row).encode("ascii")
        build_python = (
            root
            / "opt/crossforge/python"
            / row
            / "build/bin"
            / ("python" + minor)
        )
        build_python.parent.mkdir(parents=True, exist_ok=True)
        build_python.write_bytes(build_bytes)
        build_tree = FINALIZE["sdk_tree_identity"](build_python.parents[1])

        reports = {}
        target_pythons = {}
        release_sha256 = canonical_sha256(self.release)
        source = source_manifest["source"]
        release_entry_source = entry["source"]
        report_source = {
            "url": source["url"],
            "size": source["size"],
            "sha256": source["sha256"],
            "sigstore_bundle_sha256": release_entry_source["sigstore"][
                "bundle_sha256"
            ],
            "sigstore_verification": release_entry_source["sigstore"][
                "verification"
            ],
        }
        for arch, target in TARGETS.items():
            python_bytes = ("target-python-%s-%s" % (row, arch)).encode("ascii")
            target_python = (
                root
                / "opt/crossforge/python"
                / row
                / "targets"
                / target
                / "bin"
                / ("python" + minor)
            )
            target_python.parent.mkdir(parents=True, exist_ok=True)
            target_python.write_bytes(python_bytes)
            target_tree = FINALIZE["sdk_tree_identity"](target_python.parents[1])
            report = {
                "qualification_schema_version": 2,
                "report_kind": "crossforge-cpython-qualification",
                "status": "passed",
                "target": target,
                "version": entry["version"],
                "adapter": entry["adapter"],
                "release_sha256": release_sha256,
                "source": copy.deepcopy(report_source),
                "sysroot_sha256": sha256_bytes(("sysroot-" + arch).encode()),
                "python_sha256": sha256_bytes(python_bytes),
                "extension_sha256": sha256_bytes(("extension-" + arch).encode()),
                "probe_sha256": sha256_bytes(("probe-" + arch).encode()),
                "compile_report_sha256": sha256_bytes(("compile-" + arch).encode()),
                "compile": {
                    "target": target,
                    "version": entry["version"],
                    "adapter": entry["adapter"],
                    "sdk_tree": target_tree,
                    "build_python": {
                        "path": str(build_python),
                        "version": entry["version"],
                        "sha256": sha256_bytes(build_bytes),
                        "sdk_tree": build_tree,
                    },
                    "elf_audit": {
                        "bin/python" + minor: {
                            "sha256": sha256_bytes(python_bytes)
                        }
                    },
                },
                "runtime_result_sha256": {
                    "locked-sysroot": sha256_bytes(("locked-" + arch).encode()),
                    "clean-rocky": sha256_bytes(("clean-" + arch).encode()),
                },
                "executions": {
                    "locked-sysroot": {"status": "passed"},
                    "clean-rocky": {"status": "passed"},
                },
            }
            report_path = (
                root
                / "opt/crossforge/qualification/python"
                / row
                / (arch + ".json")
            )
            self.write_json(report_path, report)
            reports[arch] = {"path": report_path, "value": report}
            target_pythons[arch] = target_python
        return {
            "minor": minor,
            "entry": entry,
            "row": row,
            "root": root,
            "source_path": source_path,
            "source": source_manifest,
            "build_python": build_python,
            "reports": reports,
            "target_pythons": target_pythons,
        }

    def run_finalize(self, fixture, output=None, strict=False):
        if output is None:
            output = self.directory / (fixture["row"] + "-row.json")
        argv = [
                str(FINALIZE_PATH),
                "--root",
                str(fixture["root"]),
                "--row",
                fixture["row"],
                "--version",
                fixture["entry"]["version"],
                "--adapter",
                fixture["entry"]["adapter"],
                "--release",
                str(self.release_path),
                "--source-manifest",
                str(fixture["source_path"]),
                "--output",
                str(output),
            ]
        globals_ = FINALIZE["main"].__globals__
        validator = globals_["QUALIFICATION_VALIDATOR"]
        original = validator["validate_final_report"]
        if not strict:
            validator["validate_final_report"] = (
                lambda report, release, target, version: report
            )
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(
                stdout
            ), contextlib.redirect_stderr(stderr):
                return_code = FINALIZE["main"]()
        except Exception as error:
            return_code = 1
            stderr.write(str(error))
        finally:
            validator["validate_final_report"] = original
        return SimpleNamespace(returncode=return_code, stderr=stderr.getvalue()), output

    def test_cp311_and_cp313_source_manifests_match_exact_contract(self):
        for minor in ("3.11", "3.13"):
            with self.subTest(minor=minor):
                contract = self.source_contract(minor)
                entry = self.entry(minor)
                self.assertEqual(
                    set(contract),
                    {
                        "schema_version",
                        "kind",
                        "row",
                        "version",
                        "minor",
                        "compact",
                        "adapter",
                        "support",
                        "release_sha256",
                        "source",
                        "patches",
                    },
                )
                self.assertEqual(contract["patches"], entry.get("patches", []))
                result = self.run_verify(minor, contract)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_manifest_identity_tampering_is_rejected(self):
        mutations = {
            "adapter": lambda value: value.__setitem__("adapter", "legacy"),
            "release": lambda value: value.__setitem__("release_sha256", "0" * 64),
            "source": lambda value: value["source"].__setitem__("sha256", "0" * 64),
            "patches": lambda value: value.__setitem__("patches", []),
            "unknown": lambda value: value.__setitem__("unknown", True),
        }
        for minor in ("3.11", "3.13"):
            for name, mutate in mutations.items():
                if minor == "3.13" and name == "patches":
                    continue
                with self.subTest(minor=minor, mutation=name):
                    manifest = copy.deepcopy(self.source_contract(minor))
                    mutate(manifest)
                    result = self.run_verify(minor, manifest)
                    self.assertNotEqual(result.returncode, 0)

    def test_two_target_row_manifest_succeeds_for_cp311_and_cp313(self):
        for minor in ("3.11", "3.13"):
            with self.subTest(minor=minor):
                fixture = self.fixture(minor)
                process, output = self.run_finalize(fixture)
                self.assertEqual(process.returncode, 0, process.stderr)
                manifest = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(manifest["kind"], "crossforge-cpython-row")
                self.assertEqual(manifest["row"], fixture["row"])
                self.assertEqual(manifest["adapter"], fixture["entry"]["adapter"])
                self.assertEqual(
                    manifest["release_sha256"], canonical_sha256(self.release)
                )
                self.assertEqual(
                    manifest["source_manifest_sha256"],
                    sha256_file(fixture["source_path"]),
                )
                self.assertEqual(set(manifest["qualifications"]), set(TARGETS))
                for arch in TARGETS:
                    self.assertEqual(
                        manifest["qualifications"][arch]["report_sha256"],
                        sha256_file(fixture["reports"][arch]["path"]),
                    )

    def test_qualification_identity_tampering_is_rejected(self):
        mutations = {
            "adapter": lambda report: report.__setitem__("adapter", "legacy"),
            "embedded_adapter": lambda report: report["compile"].__setitem__(
                "adapter", "legacy"
            ),
            "release": lambda report: report.__setitem__("release_sha256", "0" * 64),
            "source": lambda report: report["source"].__setitem__("sha256", "0" * 64),
            "status": lambda report: report.__setitem__("status", "failed"),
            "target": lambda report: report.__setitem__("target", "wrong-target"),
            "hash": lambda report: report.__setitem__("python_sha256", "0" * 64),
            "unknown": lambda report: report.__setitem__("unknown", True),
            "unknown_source": lambda report: report["source"].__setitem__(
                "unknown", True
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                fixture = self.fixture("3.11")
                report = fixture["reports"]["x86_64"]["value"]
                mutate(report)
                self.write_json(fixture["reports"]["x86_64"]["path"], report)
                process, unused_output = self.run_finalize(fixture)
                self.assertNotEqual(process.returncode, 0)

    def test_target_python_byte_tampering_is_rejected(self):
        fixture = self.fixture("3.13")
        with fixture["target_pythons"]["aarch64"].open("ab") as stream:
            stream.write(b"tampered")
        process, unused_output = self.run_finalize(fixture)
        self.assertNotEqual(process.returncode, 0)

    def test_target_and_build_sdk_tree_tampering_is_rejected(self):
        for kind in ("target", "build"):
            with self.subTest(kind=kind):
                fixture = self.fixture("3.13")
                if kind == "target":
                    root = fixture["target_pythons"]["x86_64"].parents[1]
                    path = root / "include/python3.13/tampered.h"
                else:
                    root = fixture["build_python"].parents[1]
                    path = root / "lib/python3.13/tampered.py"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("tampered\n", encoding="utf-8")
                process, unused_output = self.run_finalize(fixture)
                self.assertNotEqual(process.returncode, 0)

    def test_exported_elf_digests_are_recomputed(self):
        fixture = self.fixture("3.13")
        report = fixture["reports"]["x86_64"]["value"]
        report["compile"]["elf_audit"]["bin/python3.13"]["sha256"] = "0" * 64
        self.write_json(fixture["reports"]["x86_64"]["path"], report)
        process, unused_output = self.run_finalize(fixture)
        self.assertNotEqual(process.returncode, 0)

    def test_row_manifest_binds_qualification_report_bytes(self):
        fixture = self.fixture("3.11")
        first_process, first_path = self.run_finalize(
            fixture, self.directory / "first-row.json"
        )
        self.assertEqual(first_process.returncode, 0, first_process.stderr)
        first = json.loads(first_path.read_text(encoding="utf-8"))
        report = fixture["reports"]["aarch64"]["value"]
        fixture["reports"]["aarch64"]["path"].write_text(
            json.dumps(report, sort_keys=True) + "\n", encoding="utf-8"
        )
        second_process, second_path = self.run_finalize(
            fixture, self.directory / "second-row.json"
        )
        self.assertEqual(second_process.returncode, 0, second_process.stderr)
        second = json.loads(second_path.read_text(encoding="utf-8"))
        self.assertNotEqual(
            first["qualifications"]["aarch64"]["report_sha256"],
            second["qualifications"]["aarch64"]["report_sha256"],
        )
        self.assertNotEqual(first_path.read_bytes(), second_path.read_bytes())

    def test_real_qualification_validator_rejects_skeletal_report(self):
        fixture = self.fixture("3.13")
        process, unused_output = self.run_finalize(fixture, strict=True)
        self.assertNotEqual(process.returncode, 0)

    def test_duplicate_json_keys_are_rejected_by_both_loaders(self):
        duplicate = self.directory / "duplicate.json"
        duplicate.write_text('{"row":"cp311","row":"cp313"}\n', encoding="utf-8")
        with self.assertRaises(VERIFY["RowError"]):
            VERIFY["load_json"](duplicate)
        with self.assertRaises(FINALIZE["FinalizationError"]):
            FINALIZE["load_json"](duplicate)

    def test_duplicate_key_in_qualification_report_is_rejected(self):
        fixture = self.fixture("3.13")
        report_path = fixture["reports"]["x86_64"]["path"]
        text = report_path.read_text(encoding="utf-8")
        text = text.replace(
            '  "status": "passed",',
            '  "status": "passed",\n  "status": "failed",',
            1,
        )
        report_path.write_text(text, encoding="utf-8")
        process, unused_output = self.run_finalize(fixture)
        self.assertNotEqual(process.returncode, 0)

    def test_unknown_source_manifest_key_is_rejected_by_finalizer(self):
        fixture = self.fixture("3.11")
        source = copy.deepcopy(fixture["source"])
        source["unknown"] = True
        self.write_json(fixture["source_path"], source)
        process, unused_output = self.run_finalize(fixture)
        self.assertNotEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
