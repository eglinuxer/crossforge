import hashlib
import runpy
import subprocess
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
QUALIFIER = runpy.run_path(
    str(REPOSITORY / "scripts/qualify-complete-sdk.py")
)


class CompleteSdkQualificationTests(unittest.TestCase):
    def test_launcher_builds_real_c_and_cxx_consumers_for_both_targets(self):
        commands = []

        def fake_run(arguments):
            arguments = [str(argument) for argument in arguments]
            commands.append(arguments)
            if arguments[0] == "/usr/local/bin/crossforge":
                arch = arguments[arguments.index("--target") + 1]
                triple = QUALIFIER["TARGETS"][arch]
                if "-B" in arguments:
                    build = Path(arguments[arguments.index("-B") + 1])
                    build.mkdir()
                    (build / "CMakeCache.txt").write_text(
                        "CMAKE_TOOLCHAIN_FILE:FILEPATH="
                        "/opt/crossforge/cmake/%s.cmake\n" % triple
                        + "CMAKE_C_COMPILER:FILEPATH="
                        "/opt/crossforge/targets/%s/bin/%s-gcc\n"
                        % (triple, triple),
                        encoding="utf-8",
                    )
                else:
                    build = Path(arguments[arguments.index("--build") + 1])
                    (build / "crossforge-consumer").write_bytes(
                        ("consumer-" + arch).encode("ascii")
                    )
                return ""
            binary = Path(arguments[-1])
            arch = binary.parent.name.removeprefix("build-")
            return "  Machine: %s\n" % QUALIFIER["CONSUMER_MACHINES"][arch]

        function = QUALIFIER["qualify_launcher_consumers"]
        with mock.patch.dict(function.__globals__, {"run": fake_run}):
            result = function(
                Path("/usr/local/bin/crossforge"),
                REPOSITORY / "tests/consumer",
            )
        self.assertEqual(
            [record["target"] for record in result], ["x86_64", "aarch64"]
        )
        self.assertTrue(all(record["executed"] is False for record in result))
        self.assertTrue(
            all(record["languages"] == ["c", "c++20"] for record in result)
        )
        self.assertEqual(len(commands), 6)
        launcher_commands = [
            command
            for command in commands
            if command[0] == "/usr/local/bin/crossforge"
        ]
        self.assertEqual(len(launcher_commands), 4)
        self.assertTrue(all("run" in command for command in launcher_commands))
        for record in result:
            expected = hashlib.sha256(
                ("consumer-" + record["target"]).encode("ascii")
            ).hexdigest()
            self.assertEqual(record["binary_sha256"], expected)

    def test_launcher_consumer_rejects_host_toolchain_or_target_execution(self):
        def host_cache(arguments):
            arguments = [str(argument) for argument in arguments]
            if arguments[0] != "/usr/local/bin/crossforge":
                self.fail("readelf must not run after a host compiler leak")
            if "-B" in arguments:
                build = Path(arguments[arguments.index("-B") + 1])
                build.mkdir()
                (build / "CMakeCache.txt").write_text(
                    "CMAKE_TOOLCHAIN_FILE:FILEPATH=/host/toolchain.cmake\n"
                    "CMAKE_C_COMPILER:FILEPATH=/usr/bin/gcc\n",
                    encoding="utf-8",
                )
            return ""

        function = QUALIFIER["qualify_launcher_consumers"]
        with mock.patch.dict(function.__globals__, {"run": host_cache}):
            with self.assertRaisesRegex(
                QUALIFIER["QualificationError"], "CMake toolchain differs"
            ):
                function(
                    Path("/usr/local/bin/crossforge"),
                    REPOSITORY / "tests/consumer",
                )

    def test_qualification_commands_have_a_hard_timeout(self):
        with mock.patch.object(
            QUALIFIER["subprocess"],
            "run",
            side_effect=subprocess.TimeoutExpired(["cmake"], 300),
        ):
            with self.assertRaisesRegex(
                QUALIFIER["QualificationError"], "timed out after 300 seconds"
            ):
                QUALIFIER["run"](["cmake", "--version"])


if __name__ == "__main__":
    unittest.main()
