import io
import runpy
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
PROBE = runpy.run_path(str(REPOSITORY / "tests/python/runtime_probe.py"))


class PythonRuntimeProbeContractTests(unittest.TestCase):
    def test_absent_and_zero_gil_policies_are_distinct(self):
        PROBE["validate_gil_policy"]({}, "3.11", "absent")
        PROBE["validate_gil_policy"]({}, "3.12", "absent")
        PROBE["validate_gil_policy"](
            {"Py_GIL_DISABLED": 0}, "3.13", "zero"
        )

        for variables, minor, policy in (
            ({"Py_GIL_DISABLED": 0}, "3.11", "absent"),
            ({"Py_GIL_DISABLED": 0}, "3.12", "absent"),
            ({}, "3.13", "zero"),
            ({"Py_GIL_DISABLED": 1}, "3.13", "zero"),
            ({"Py_GIL_DISABLED": False}, "3.13", "zero"),
            ({"Py_GIL_DISABLED": 0.0}, "3.13", "zero"),
            ({}, "3.13", "unsupported"),
        ):
            with self.subTest(variables=variables, minor=minor, policy=policy):
                with self.assertRaises(PROBE["ProbeError"]):
                    PROBE["validate_gil_policy"](variables, minor, policy)

    def test_abi_integer_predicate_rejects_bool_and_float(self):
        self.assertTrue(PROBE["is_exact_integer"](0, 0))
        self.assertFalse(PROBE["is_exact_integer"](False, 0))
        self.assertFalse(PROBE["is_exact_integer"](0.0, 0))

    def test_absent_zstd_policy_proves_both_imports_are_unavailable(self):
        def missing_module(name):
            raise ModuleNotFoundError(name)

        with mock.patch.object(
            PROBE["importlib"], "import_module", side_effect=missing_module
        ):
            self.assertEqual(
                PROBE["exercise_zstd"]("absent"),
                {
                    "available": False,
                    "policy": "absent",
                    "rejected_imports": ["_zstd", "compression.zstd"],
                },
            )

    def test_absent_zstd_policy_rejects_an_available_module(self):
        def import_module(name):
            if name == "_zstd":
                return object()
            raise ModuleNotFoundError(name)

        with mock.patch.object(
            PROBE["importlib"], "import_module", side_effect=import_module
        ):
            with self.assertRaisesRegex(
                PROBE["ProbeError"], "unexpectedly available"
            ):
                PROBE["exercise_zstd"]("absent")

    def test_required_zstd_policy_exercises_every_required_surface(self):
        events = []
        prefix = b"zstd-frame:"
        test_case = self

        class FakeZstdError(Exception):
            pass

        class FakeWorkers:
            @staticmethod
            def bounds():
                return (0, 256)

        class FakeCompressionParameter:
            nb_workers = FakeWorkers()

        class FakeCompressor:
            def compress(self, value):
                events.append("streaming")
                return prefix + value

            @staticmethod
            def flush():
                return b""

        class FakeZstd:
            zstd_version = "1.5.7"
            zstd_version_info = (1, 5, 7)
            ZstdError = FakeZstdError
            CompressionParameter = FakeCompressionParameter
            ZstdCompressor = FakeCompressor

            @staticmethod
            def compress(value, level=None, options=None, zstd_dict=None):
                if options is not None:
                    self.assertEqual(
                        options, {FakeCompressionParameter.nb_workers: 1}
                    )
                    events.append("multithread")
                elif zstd_dict is not None:
                    self.assertEqual(level, 6)
                    events.append("dictionary-roundtrip")
                else:
                    events.append("one-shot")
                return prefix + value

            @staticmethod
            def decompress(value, zstd_dict=None, options=None):
                if not value.startswith(prefix):
                    events.append("corrupt")
                    raise FakeZstdError("invalid frame")
                return value[len(prefix):]

            @staticmethod
            def train_dict(samples, size):
                self.assertEqual(len(samples), 300)
                self.assertEqual(size, 3 * 1024)
                events.append("train-dict")
                return object()

            @staticmethod
            def finalize_dict(trained, samples, size, level):
                self.assertIsNotNone(trained)
                self.assertEqual(len(samples), 300)
                self.assertEqual((size, level), (200 * 1024, 6))
                events.append("finalize-dict")
                return object()

        class FakeTarInfo:
            def __init__(self, name):
                self.name = name
                self.size = None

        class FakeTarArchive:
            def __init__(self, fileobj, mode):
                self.fileobj = fileobj
                self.mode = mode

            def __enter__(self):
                events.append("tar-" + self.mode)
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def addfile(self, member, fileobj):
                test_case.assertEqual(member.name, "payload.bin")
                test_case.assertEqual(member.size, len(PROBE["PAYLOAD"]))
                self.fileobj.write(fileobj.read())

            def extractfile(self, name):
                test_case.assertEqual(name, "payload.bin")
                return io.BytesIO(self.fileobj.getvalue())

        class FakeTarfile:
            TarInfo = FakeTarInfo

            @staticmethod
            def open(fileobj, mode):
                return FakeTarArchive(fileobj, mode)

        class FakeZipArchive:
            def __init__(self, fileobj, mode, compression=None):
                self.fileobj = fileobj
                self.mode = mode
                if mode == "w":
                    test_case.assertEqual(compression, 93)

            def __enter__(self):
                events.append("zip-" + self.mode)
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def writestr(self, name, value):
                test_case.assertEqual(name, "payload.bin")
                self.fileobj.write(value)

            def read(self, name):
                test_case.assertEqual(name, "payload.bin")
                return self.fileobj.getvalue()

        class FakeZipfile:
            ZIP_ZSTANDARD = 93
            ZipFile = FakeZipArchive

        imported_modules = []

        def import_module(name):
            imported_modules.append(name)
            if name == "compression.zstd":
                return FakeZstd
            return object()

        probe_globals = PROBE["exercise_zstd"].__globals__
        with mock.patch.object(
            PROBE["importlib"], "import_module", side_effect=import_module
        ), mock.patch.dict(
            probe_globals,
            {"tarfile": FakeTarfile, "zipfile": FakeZipfile},
        ):
            evidence = PROBE["exercise_zstd"]("required")
            imports = PROBE["exercise_imports"]("required")

        self.assertEqual(
            evidence,
            {
                "available": True,
                "corrupt_error": "ZstdError",
                "dictionary": {"finalized": True, "trained": True},
                "multithread": {"nb_workers": 1, "supported": True},
                "payload_sha256": (
                    "dd1fc53b1dfcac3378b57b9b8b2723c16f2b6aad628c940b09f6904fba3957a2"
                ),
                "policy": "required",
                "roundtrips": [
                    "dictionary",
                    "multithread",
                    "one-shot",
                    "streaming",
                    "tarfile",
                    "zipfile",
                ],
                "version": "1.5.7",
                "version_info": [1, 5, 7],
            },
        )
        self.assertEqual(imports[-2:], ["_zstd", "compression.zstd"])
        self.assertEqual(
            imports,
            list(PROBE["REQUIRED_IMPORTS"])
            + ["_zstd", "compression.zstd"],
        )
        self.assertEqual(imported_modules[-len(imports):], imports)
        for expected in (
            "corrupt",
            "dictionary-roundtrip",
            "finalize-dict",
            "multithread",
            "streaming",
            "tar-r:zst",
            "tar-w:zst",
            "train-dict",
            "zip-r",
            "zip-w",
        ):
            self.assertIn(expected, events)


if __name__ == "__main__":
    unittest.main()
