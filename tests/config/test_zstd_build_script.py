import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]


@unittest.skipUnless(
    all(shutil.which(tool) for tool in ("cc", "ar", "ranlib")),
    "native archive tools are unavailable",
)
class ZstdBuildScriptTests(unittest.TestCase):
    def test_installed_archive_is_reproducible_before_evidence_is_created(self):
        script = (REPOSITORY / "scripts/build-zstd.sh").read_text()
        start = script.index('install -m 0644 "$build_directory/lib/libzstd.a"')
        end = script.index('install -m 0644 "$source_directory/LICENSE"', start)
        install_archive = script[start:end]
        self.assertLess(end, script.index('probe_source='))
        self.assertLess(end, script.index('archive_sha=$(sha256sum'))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build"
            (build / "lib").mkdir(parents=True)
            obj = root / "fixture.o"
            subprocess.run(
                ["cc", "-xc", "-c", "-o", str(obj), "-"],
                input="int fixture(void) { return 1; }\n",
                text=True,
                check=True,
            )
            archive = build / "lib/libzstd.a"
            subprocess.run(["ar", "rcD", str(archive), str(obj)], check=True)
            canonical = archive.read_bytes()
            self.assertEqual(canonical[8:24].strip(), b"/")
            # Reproduce the cached host archive: identical object members,
            # but a wall-clock timestamp in its symbol-index header.
            installed = []
            for timestamp in (b"1788937788", b"1788945500"):
                archive.write_bytes(
                    canonical[:24] + timestamp.ljust(12) + canonical[36:]
                )
                prefix = root / timestamp.decode()
                (prefix / "lib").mkdir(parents=True)
                subprocess.run(
                    ["bash", "-Eeuo", "pipefail", "-c", install_archive],
                    env=dict(
                        os.environ,
                        build_directory=str(build),
                        prefix=str(prefix),
                        ranlib=shutil.which("ranlib"),
                    ),
                    check=True,
                )
                output = prefix / "lib/libzstd.a"
                installed.append(output.read_bytes())
                member = subprocess.check_output(["ar", "p", str(output), obj.name])
                self.assertEqual(member, obj.read_bytes())
            self.assertEqual(installed, [canonical, canonical])


if __name__ == "__main__":
    unittest.main()
