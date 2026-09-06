import ast
import copy
import hashlib
import io
import json
import runpy
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
FETCHER_PATH = REPOSITORY / "scripts/fetch-release-source.py"
FETCHER = runpy.run_path(str(FETCHER_PATH))
READER = runpy.run_path(str(REPOSITORY / "scripts/release_component.py"))


def python_component(payload, index="47"):
    version = "3.12.14"
    base = "/python/versions/%s" % index
    return {
        "$schema": "https://crossforge.dev/schemas/release-component.schema.json",
        "schema_version": 1,
        "kind": "crossforge-release-component",
        "component": "python/cp312-source",
        "scope": "build",
        "dependencies": [],
        "materials": [
            {
                "path": base + "/source/sha256",
                "value": hashlib.sha256(payload).hexdigest(),
            },
            {"path": base + "/source/size", "value": len(payload)},
            {"path": base + "/source/status", "value": "locked"},
            {
                "path": base + "/source/url",
                "value": "https://www.python.org/ftp/python/%s/Python-%s.tar.xz"
                % (version, version),
            },
            {"path": base + "/version", "value": version},
        ],
    }


class ComponentSourceFetcherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "component.json"
        self.output = self.directory / "Python.tar.xz"
        self.payload = b"content-locked-python-source"
        self.document = python_component(self.payload)
        self.digest = self.write_component(self.document)

    def tearDown(self):
        self.temporary.cleanup()

    def write_component(self, document):
        self.path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return READER["canonical_sha256"](document)

    def source(self, document=None, **overrides):
        if document is not None:
            digest = self.write_component(document)
        else:
            digest = self.digest
        arguments = {
            "path": self.path,
            "expected_component": "python/cp312-source",
            "expected_scope": "build",
            "expected_sha256": digest,
            "source_kind": "python",
            "version": "3.12.14",
        }
        arguments.update(overrides)
        return FETCHER["source_for_component"](**arguments)

    def test_dynamic_python_pointer_index_is_not_hard_coded(self):
        source = self.source()
        self.assertEqual(source["status"], "locked")
        self.assertEqual(source["size"], len(self.payload))
        self.assertEqual(source["sha256"], hashlib.sha256(self.payload).hexdigest())

    def test_component_mode_needs_only_fetcher_reader_and_projection(self):
        scripts = self.directory / "isolated/scripts"
        scripts.mkdir(parents=True)
        isolated_fetcher = scripts / "fetch-release-source.py"
        shutil.copy2(str(FETCHER_PATH), str(isolated_fetcher))
        shutil.copy2(
            str(REPOSITORY / "scripts/release_component.py"),
            str(scripts / "release_component.py"),
        )
        module = runpy.run_path(str(isolated_fetcher))
        source = module["source_for_component"](
            self.path,
            "python/cp312-source",
            "build",
            self.digest,
            "python",
            "3.12.14",
        )
        self.assertEqual(source["size"], len(self.payload))
        self.assertFalse((scripts / "validate-release.py").exists())

    def test_tracked_source_components_are_supported(self):
        binding = json.loads(
            (REPOSITORY / "config/generated/release-binding.json").read_text(
                encoding="utf-8"
            )
        )
        records = {item["component"]: item for item in binding["components"]}
        cases = (
            ("gcc", "sources/gcc", None),
            ("binutils", "sources/binutils", None),
            ("python", "python/cp312-source", "3.12.14"),
            ("zstd", "sources/zstd", "1.5.7"),
            ("cmake", "sources/cmake", None),
            ("vcpkg-tool", "sources/vcpkg", None),
        )
        for source_kind, component, version in cases:
            with self.subTest(component=component):
                record = records[component]
                source = FETCHER["source_for_component"](
                    REPOSITORY / record["path"],
                    component,
                    record["scope"],
                    record["canonical_sha256"],
                    source_kind,
                    version,
                )
                self.assertEqual(source["status"], "locked")
                self.assertGreater(source["size"], 0)
                self.assertRegex(source["sha256"], r"^[0-9a-f]{64}$")

    def test_qemu_supply_archives_are_available_only_in_release_mode(self):
        release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        cases = (
            ("qemu", release["qemu"]["executor"]["source"]["archive"]),
            (
                "binfmt",
                release["qemu"]["executor"]["provenance"]["builder_source"],
            ),
        )
        for source_kind, expected in cases:
            with self.subTest(source_kind=source_kind):
                source = FETCHER["load_maintenance_source"](
                    REPOSITORY / "config/release.json",
                    REPOSITORY / "config/schemas/release.schema.json",
                    source_kind,
                    None,
                )
                self.assertEqual(source, expected)
                with self.assertRaisesRegex(
                    FETCHER["ValidationError"], "unsupported"
                ):
                    FETCHER["source_for_component"](
                        self.path,
                        "python/cp312-source",
                        "build",
                        self.digest,
                        source_kind,
                    )

    def test_wrong_component_digest_scope_kind_version_and_row_are_rejected(self):
        wrong_digest = ("0" if self.digest[0] != "0" else "1") + self.digest[1:]
        cases = (
            {"expected_component": "python/cp313-source"},
            {"expected_sha256": wrong_digest},
            {"expected_scope": "qualification"},
            {"source_kind": "gcc", "version": None},
            {"version": "3.12.13"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(FETCHER["ValidationError"]):
                    self.source(**overrides)

        row = copy.deepcopy(self.document)
        row["component"] = "python/cp313-source"
        with self.assertRaisesRegex(FETCHER["ValidationError"], "row differs"):
            self.source(row, expected_component="python/cp313-source")

    def test_ambiguous_or_missing_material_set_is_rejected(self):
        ambiguous = copy.deepcopy(self.document)
        ambiguous["materials"].append(
            {"path": "/python/versions/99/version", "value": "3.12.14"}
        )
        ambiguous["materials"].sort(key=lambda material: material["path"])
        with self.assertRaisesRegex(FETCHER["ValidationError"], "ambiguous"):
            self.source(ambiguous)

        for missing_path in (
            "/python/versions/47/version",
            "/python/versions/47/source/status",
            "/python/versions/47/source/url",
            "/python/versions/47/source/sha256",
            "/python/versions/47/source/size",
        ):
            with self.subTest(missing_path=missing_path):
                missing = copy.deepcopy(self.document)
                missing["materials"] = [
                    material
                    for material in missing["materials"]
                    if material["path"] != missing_path
                ]
                with self.assertRaises(FETCHER["ValidationError"]):
                    self.source(missing)

    def test_material_types_and_duplicate_paths_are_rejected(self):
        for value in ("27", True, 1.5, None):
            with self.subTest(value=value):
                wrong_type = copy.deepcopy(self.document)
                next(
                    material
                    for material in wrong_type["materials"]
                    if material["path"].endswith("/source/size")
                )["value"] = value
                with self.assertRaisesRegex(
                    FETCHER["ValidationError"], "JSON type integer"
                ):
                    self.source(wrong_type)

        duplicate = copy.deepcopy(self.document)
        duplicate["materials"].append(copy.deepcopy(duplicate["materials"][0]))
        duplicate["materials"].sort(key=lambda material: material["path"])
        with self.assertRaisesRegex(FETCHER["ValidationError"], "repeats"):
            self.source(duplicate)

    def test_duplicate_json_keys_are_rejected_before_digest_trust(self):
        payload = json.dumps(self.document, sort_keys=True)
        payload = payload.replace(
            '"scope": "build"',
            '"scope": "build", "scope": "build"',
        )
        self.path.write_text(payload, encoding="utf-8")
        with self.assertRaisesRegex(FETCHER["ValidationError"], "duplicate JSON key"):
            self.source()

    def test_locked_status_url_sha_and_positive_size_are_required(self):
        mutations = (
            ("status", "pending"),
            ("url", "http://example.invalid/Python.tar.xz"),
            ("sha256", "0" * 63),
            ("size", 0),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.document)
                next(
                    material
                    for material in candidate["materials"]
                    if material["path"].endswith("/source/" + field)
                )["value"] = value
                with self.assertRaises(FETCHER["ValidationError"]):
                    self.source(candidate)

    def test_component_cli_requires_trust_inputs_and_is_exclusive_with_release(self):
        self.output.write_bytes(self.payload)
        valid = [
            "python",
            "--version",
            "3.12.14",
            "--output",
            str(self.output),
            "--component-file",
            str(self.path),
            "--expected-component",
            "python/cp312-source",
            "--expected-scope",
            "build",
            "--expected-sha256",
            self.digest,
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(FETCHER["main"](valid), 0)
        self.assertIn("cached:", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

        required_prefix = valid[: valid.index("--expected-component")]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(FETCHER["main"](required_prefix), 1)

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                FETCHER["main"](
                    valid
                    + ["--config", str(REPOSITORY / "config/release.json")]
                )

        maintenance_with_trust = [
            "gcc",
            "--output",
            str(self.output),
            "--expected-component",
            "sources/gcc",
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(FETCHER["main"](maintenance_with_trust), 1)

    def test_fetch_rejects_a_symlink_in_a_missing_output_ancestor(self):
        outside = self.directory / "outside"
        outside.mkdir()
        safe = self.directory / "safe"
        safe.mkdir()
        (safe / "redirect").symlink_to(outside, target_is_directory=True)
        output = safe / "redirect/new/source.tar.xz"
        source = {
            "url": "https://example.invalid/source.tar.xz",
            "size": len(self.payload),
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        }
        with self.assertRaisesRegex(FETCHER["ValidationError"], "ancestor"):
            FETCHER["fetch"](source, output)
        self.assertFalse((outside / "new").exists())

    def test_fetch_rejects_a_valid_existing_file_through_symlink_ancestor(self):
        outside = self.directory / "existing-outside"
        outside.mkdir()
        (outside / "source.tar.xz").write_bytes(self.payload)
        redirect = self.directory / "existing-redirect"
        redirect.symlink_to(outside, target_is_directory=True)
        source = {
            "url": "https://example.invalid/source.tar.xz",
            "size": len(self.payload),
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        }
        with self.assertRaisesRegex(FETCHER["ValidationError"], "ancestor"):
            FETCHER["fetch"](source, redirect / "source.tar.xz")

    def test_transient_network_errors_are_retried_then_atomically_committed(self):
        output = self.directory / "retry/source.tar.xz"
        source = {
            "url": "https://example.invalid/source.tar.xz",
            "size": len(self.payload),
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        }
        calls = []

        class Response:
            headers = {"Content-Length": str(len(self.payload))}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                return False

            def read(self, _size):
                payload, self.payload = self.payload, b""
                return payload

        class FailingResponse(Response):
            def read(self, _size):
                if self.payload:
                    payload, self.payload = self.payload[:5], b""
                    return payload
                raise ConnectionResetError("connection reset")

        original_urlopen = FETCHER["urllib"].request.urlopen
        original_sleep = FETCHER["time"].sleep

        def urlopen(_request, timeout):
            self.assertEqual(timeout, 60)
            calls.append(timeout)
            if len(calls) == 1:
                raise urllib.error.URLError("TLS handshake reset")
            if len(calls) == 2:
                return FailingResponse(self.payload)
            return Response(self.payload)

        FETCHER["urllib"].request.urlopen = urlopen
        FETCHER["time"].sleep = lambda delay: self.assertEqual(delay, 0)
        stderr = io.StringIO()
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                FETCHER["fetch"](source, output, attempts=5, retry_delay=0)
        finally:
            FETCHER["urllib"].request.urlopen = original_urlopen
            FETCHER["time"].sleep = original_sleep
        self.assertEqual(len(calls), 3)
        self.assertEqual(output.read_bytes(), self.payload)
        self.assertEqual(stderr.getvalue().count("retrying source download"), 2)
        self.assertEqual(list(output.parent.glob("*.part")), [])

    def test_identity_failure_is_not_retried(self):
        output = self.directory / "identity/source.tar.xz"
        source = {
            "url": "https://example.invalid/source.tar.xz",
            "size": len(self.payload),
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        }
        wrong_payload = b"x" * len(self.payload)
        calls = []

        class Response:
            headers = {"Content-Length": str(len(wrong_payload))}

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                return False

            def read(self, _size):
                payload, self.payload = self.payload, b""
                return payload

        def urlopen(_request, _timeout=None, **_kwargs):
            calls.append(True)
            response = Response()
            response.payload = wrong_payload
            return response

        original_urlopen = FETCHER["urllib"].request.urlopen
        FETCHER["urllib"].request.urlopen = urlopen
        try:
            with self.assertRaisesRegex(
                FETCHER["ValidationError"], "SHA256 mismatch"
            ):
                FETCHER["fetch"](source, output, attempts=5, retry_delay=0)
        finally:
            FETCHER["urllib"].request.urlopen = original_urlopen
        self.assertEqual(len(calls), 1)
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.glob("*.part")), [])

    def test_fetcher_is_python36_syntax_compatible(self):
        ast.parse(
            FETCHER_PATH.read_text(encoding="utf-8"),
            filename=str(FETCHER_PATH),
            feature_version=(3, 6),
        )


if __name__ == "__main__":
    unittest.main()
