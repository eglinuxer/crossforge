import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
EXEC_OPERATIONS = (
    "execve",
    "execv",
    "execvp",
    "execvpe",
    "execl",
    "execlp",
    "execle",
    "fexecve",
    "execveat",
    "posix_spawn",
    "posix_spawnp",
)
LOADER_OPERATIONS = ("dlopen", "dlmopen")


class TargetArtifactGuardTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("gcc"), "requires a host C compiler")
    @unittest.skipUnless(hasattr(os, "posix_spawn"), "requires os.posix_spawn")
    def test_dynamic_libc_paths_are_denied_and_audited(self):
        machines = {"x86_64": "62", "aarch64": "183"}
        machine = machines.get(platform.machine())
        if machine is None:
            self.skipTest("unsupported test host architecture")

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            guarded = directory / "guarded"
            host = directory / "host"
            guarded.mkdir()
            host.mkdir()
            guard = host / "deny-target-artifact.so"
            executable = guarded / "target-exec-canary"
            shared_object = guarded / "target-dlopen-canary.so"
            allowed_executable = host / "allowed-exec-canary"
            allowed_shared_object = host / "allowed-dlopen-canary.so"
            helper = host / "target-artifact-canary"
            audit = host / "audit.log"

            self.compile(
                "-shared",
                "-fPIC",
                REPOSITORY / "scripts/deny-target-exec.c",
                "-ldl",
                "-o",
                guard,
            )
            self.compile(
                REPOSITORY / "scripts/target-exec-canary.c",
                "-o",
                executable,
            )
            self.compile(
                "-shared",
                "-fPIC",
                REPOSITORY / "scripts/target-exec-canary.c",
                "-o",
                shared_object,
            )
            self.compile(
                REPOSITORY / "scripts/target-exec-canary.c",
                "-o",
                allowed_executable,
            )
            self.compile(
                "-shared",
                "-fPIC",
                REPOSITORY / "scripts/target-exec-canary.c",
                "-o",
                allowed_shared_object,
            )
            self.compile(
                REPOSITORY / "scripts/target-artifact-canary.c",
                "-ldl",
                "-o",
                helper,
            )
            audit.write_text("", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "CROSSFORGE_DENY_EXEC_ROOTS": str(guarded),
                    "CROSSFORGE_DENY_EXEC_MACHINE": machine,
                    "CROSSFORGE_DENY_EXEC_LOG": str(audit),
                    "LD_PRELOAD": str(guard),
                }
            )

            denied = []
            denied.append(
                self.run_process(
                    ["/bin/bash", "-c", '"$1"', "crossforge-test", executable],
                    environment,
                )
            )
            denied.append(
                self.run_process(
                    [
                        sys.executable,
                        "-c",
                        "import os,sys; os.execv(sys.argv[1], [sys.argv[1]])",
                        executable,
                    ],
                    environment,
                )
            )
            denied.append(
                self.run_process(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,sys\n"
                            "try:\n"
                            " p=os.posix_spawn(sys.argv[1],[sys.argv[1]],os.environ)\n"
                            "except OSError: raise SystemExit(77)\n"
                            "_,s=os.waitpid(p,0)\n"
                            "raise SystemExit(os.WEXITSTATUS(s) if os.WIFEXITED(s) else 79)"
                        ),
                        executable,
                    ],
                    environment,
                )
            )
            helper_exec_operations = (
                "execvp",
                "execvpe",
                "execl",
                "execlp",
                "execle",
                "fexecve",
                "execveat",
                "posix_spawnp",
            )
            for operation in helper_exec_operations:
                denied.append(
                    self.run_process([helper, operation, executable], environment)
                )
            for operation in LOADER_OPERATIONS:
                denied.append(
                    self.run_process([helper, operation, shared_object], environment)
                )
            self.assertTrue(all(result.returncode != 0 for result in denied))

            expected = [
                "execve\t" + str(executable),
                "execv\t" + str(executable),
                "posix_spawn\t" + str(executable),
            ]
            expected.extend(
                operation + "\t" + str(executable)
                for operation in helper_exec_operations
            )
            expected.extend(
                operation + "\t" + str(shared_object)
                for operation in LOADER_OPERATIONS
            )
            self.assertEqual(
                {line.split("\t", 1)[0] for line in expected},
                set(EXEC_OPERATIONS + LOADER_OPERATIONS),
            )
            self.assertEqual(audit.read_text(encoding="utf-8").splitlines(), expected)

            allowed = []
            allowed.append(
                self.run_process(
                    ["/bin/bash", "-c", '"$1"', "crossforge-test", allowed_executable],
                    environment,
                )
            )
            allowed.append(
                self.run_process(
                    [
                        sys.executable,
                        "-c",
                        "import os,sys; os.execv(sys.argv[1], [sys.argv[1]])",
                        allowed_executable,
                    ],
                    environment,
                )
            )
            allowed.append(
                self.run_process(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,sys\n"
                            "p=os.posix_spawn(sys.argv[1],[sys.argv[1]],os.environ)\n"
                            "_,s=os.waitpid(p,0)\n"
                            "raise SystemExit(os.WEXITSTATUS(s) if os.WIFEXITED(s) else 79)"
                        ),
                        allowed_executable,
                    ],
                    environment,
                )
            )
            for operation in helper_exec_operations:
                allowed.append(
                    self.run_process(
                        [helper, operation, allowed_executable], environment
                    )
                )
            for operation in LOADER_OPERATIONS:
                allowed.append(
                    self.run_process(
                        [helper, operation, allowed_shared_object], environment
                    )
                )
            self.assertTrue(all(result.returncode == 0 for result in allowed))
            self.assertEqual(audit.read_text(encoding="utf-8").splitlines(), expected)

    @staticmethod
    def run_process(arguments, environment):
        return subprocess.run(
            [str(argument) for argument in arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    @staticmethod
    def compile(*arguments):
        subprocess.run(
            ["gcc", "-O2", "-Wall", "-Wextra", "-Werror"]
            + [str(argument) for argument in arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


if __name__ == "__main__":
    unittest.main()
