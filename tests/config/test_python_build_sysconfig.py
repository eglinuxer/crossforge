import ast
import copy
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/verify-python-build-sysconfig.py"
VERIFIER = runpy.run_path(str(SCRIPT))


class PythonBuildSysconfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source_lib = root / "source/Lib"
        self.target_directory = root / "build/lib.linux-x86_64-3.9"
        self.expected = {
            "target": "x86_64-unknown-linux-gnu",
            "CC": "/opt/crossforge/targets/x86_64-unknown-linux-gnu/bin/"
            "x86_64-unknown-linux-gnu-gcc "
            "--sysroot=/opt/crossforge/sysroots/el8/x86_64",
            "AR": "/opt/crossforge/targets/x86_64-unknown-linux-gnu/bin/"
            "x86_64-unknown-linux-gnu-ar",
            "LDSHARED": "/opt/crossforge/targets/x86_64-unknown-linux-gnu/bin/"
            "x86_64-unknown-linux-gnu-gcc "
            "--sysroot=/opt/crossforge/sysroots/el8/x86_64 -shared "
            "-Wl,-z,relro,-z,now -Wl,-z,relro,-z,now",
            "MULTIARCH": "x86_64-linux-gnu",
            "SOABI": "cpython-39-x86_64-linux-gnu",
            "EXT_SUFFIX": ".cpython-39-x86_64-linux-gnu.so",
        }
        self.canonical = {
            "AR": self.expected["AR"],
            "CC": self.expected["CC"],
            "CONFIG_ARGS": "--build=x86_64-pc-linux-gnu "
            "--host=x86_64-unknown-linux-gnu",
            "EXT_SUFFIX": self.expected["EXT_SUFFIX"],
            "LDSHARED": self.expected["LDSHARED"],
            "MULTIARCH": self.expected["MULTIARCH"],
            "SOABI": self.expected["SOABI"],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def validate(self, canonical=None, legacy=None, sys_path=None):
        canonical = self.canonical if canonical is None else canonical
        legacy = canonical if legacy is None else legacy
        sys_path = [str(self.source_lib), "/usr/lib/python3.9"] if sys_path is None else sys_path
        return VERIFIER["validate"](
            canonical,
            legacy,
            sys_path,
            self.target_directory,
            self.source_lib,
            self.expected,
        )

    def test_matching_target_configuration_is_accepted(self):
        self.assertEqual(self.validate(), self.canonical)

    def test_host_fallback_and_target_path_leaks_are_rejected(self):
        host = copy.deepcopy(self.canonical)
        host["CC"] = "x86_64-unknown-linux-gnu-gcc -pthread"
        mutations = (
            {"legacy": host},
            {"canonical": host, "legacy": host},
            {
                "sys_path": [
                    str(self.source_lib),
                    str(self.target_directory),
                ]
            },
            {"sys_path": ["/usr/lib/python3.9"]},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(VERIFIER["VerificationError"]):
                    self.validate(**mutation)

    def test_fields_and_configure_host_are_fail_closed(self):
        cases = []
        missing = copy.deepcopy(self.canonical)
        missing.pop("LDSHARED")
        cases.append(missing)
        empty = copy.deepcopy(self.canonical)
        empty["LDSHARED"] = ""
        cases.append(empty)
        host_linker = copy.deepcopy(self.canonical)
        host_linker["LDSHARED"] = "/usr/bin/gcc -shared"
        cases.append(host_linker)
        duplicate_host = copy.deepcopy(self.canonical)
        duplicate_host["CONFIG_ARGS"] += " --host=x86_64-unknown-linux-gnu"
        cases.append(duplicate_host)
        wrong_host = copy.deepcopy(self.canonical)
        wrong_host["CONFIG_ARGS"] = "--host=aarch64-unknown-linux-gnu"
        cases.append(wrong_host)
        for canonical in cases:
            with self.subTest(canonical=canonical):
                with self.assertRaises(VERIFIER["VerificationError"]):
                    self.validate(canonical=canonical, legacy=canonical)

    def test_verifier_is_python39_syntax_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 9),
        )


if __name__ == "__main__":
    unittest.main()
