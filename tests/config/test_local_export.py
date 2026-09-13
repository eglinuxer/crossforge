"""A stuck local transfer must not hang its consumer or supply partial files."""

import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import tarfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from crossforge_internal import local_export
from crossforge_internal.identity import IdentityError
sys.path.pop(0)


class LocalExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.graph = {"target": {"extract": {"context": ".", "platforms": ["linux/amd64"],
            "contexts": {"artifact": "oci-layout:///input@sha256:" + "a" * 64},
            "dockerfile-inline": "# syntax=fixed\nFROM scratch\nCOPY --from=artifact [\"/component/\", \"/component/\"]\n",
            "output": [{"type": "local", "dest": str(self.root / "files")}]}}}

    def archive(self, path, entries):
        with tarfile.open(str(path), 'w') as archive:
            for member, data in entries:
                archive.addfile(member, io.BytesIO(data) if data is not None else None)

    def file(self, name, data=b'complete', mode=0o640):
        member = tarfile.TarInfo(name)
        member.mode = mode
        member.size = len(data)
        return member, data

    def test_timeout_retries_same_input_into_new_directory(self):
        original = copy.deepcopy(self.graph)
        attempts = []
        def transfer(command, directory):
            recipe = json.loads(Path(command[command.index("-f") + 1]).read_text())
            attempts.append(recipe)
            dest = Path(recipe["target"]["extract"]["output"][0]["dest"])
            self.assertIn("--allow=fs.write=" + str(dest), command)
            self.assertEqual(recipe["target"]["extract"]["output"][0]["type"], "tar")
            if len(attempts) == 1:
                dest.write_bytes(b"partial tar stream")
                raise subprocess.TimeoutExpired(command, 600)
            self.assertFalse(dest.exists())
            self.archive(dest, [self.file('complete')])
        with mock.patch.object(local_export, "execute", side_effect=transfer):
            result = local_export.extract(self.graph, "extract", self.root, ["docker", "buildx", "bake"])
        self.assertEqual(result, self.root / "files-attempt2")
        self.assertEqual((self.root / "export.tar").read_bytes(), b"partial tar stream")
        self.assertFalse(any((self.root / 'files').iterdir()))
        self.assertTrue((result / "complete").is_file())
        self.assertTrue((self.root / "export-timeout-1.json").is_file())
        attempts[1]["target"]["extract"]["output"] = attempts[0]["target"]["extract"]["output"]
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(self.graph, original)

    def test_completed_transport_preserves_files_modes_and_links(self):
        directory = tarfile.TarInfo('private')
        directory.type, directory.mode = tarfile.DIRTYPE, 0o700
        link = tarfile.TarInfo('link')
        link.type, link.linkname = tarfile.SYMTYPE, 'private/data'
        hardlink = tarfile.TarInfo('hardlink')
        hardlink.type, hardlink.linkname, hardlink.mode = tarfile.LNKTYPE, 'private/data', 0o600
        path = self.root / 'input.tar'
        self.archive(path, [(link, None), (hardlink, None), (directory, None), self.file('private/data', mode=0o600)])
        destination = self.root / 'unpacked'
        destination.mkdir()
        local_export.unpack(path, destination)
        self.assertEqual((destination / 'private/data').read_bytes(), b'complete')
        self.assertEqual((destination / 'private').stat().st_mode & 0o777, 0o700)
        self.assertEqual((destination / 'private/data').stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.readlink(destination / 'link'), 'private/data')
        self.assertEqual((destination / 'hardlink').stat().st_ino, (destination / 'private/data').stat().st_ino)

    def test_unsafe_or_ambiguous_archives_fail_before_extracting_files(self):
        for case in ('absolute', 'parent', 'duplicate', 'symlink-parent', 'hardlink-parent',
                     'absolute-link', 'escaping-link', 'missing-hardlink', 'device'):
            with self.subTest(case=case):
                entries = [self.file('safe')]
                if case in ('absolute', 'parent', 'duplicate'):
                    entries.append(self.file({'absolute': '/outside', 'parent': '../outside', 'duplicate': './safe'}[case]))
                else:
                    member = tarfile.TarInfo('link')
                    member.type = tarfile.SYMTYPE
                    member.linkname = 'safe'
                    if case == 'absolute-link':
                        member.linkname = '/outside'
                    elif case == 'escaping-link':
                        member.linkname = '../outside'
                    elif case == 'missing-hardlink':
                        member.type, member.linkname = tarfile.LNKTYPE, 'missing'
                    elif case == 'device':
                        member.type = tarfile.CHRTYPE
                    elif case == 'hardlink-parent':
                        member.type = tarfile.LNKTYPE
                    entries.append((member, None))
                    if case.endswith('-parent'):
                        entries.append(self.file('link/child'))
                path = self.root / (case + '.tar')
                self.archive(path, entries)
                destination = self.root / case
                destination.mkdir()
                with self.assertRaises(IdentityError):
                    local_export.unpack(path, destination)
                self.assertFalse(any(destination.iterdir()))

    def test_truncated_tar_is_not_retried_or_returned(self):
        def transfer(command, directory):
            recipe = json.loads(Path(command[command.index('-f') + 1]).read_text())
            Path(recipe['target']['extract']['output'][0]['dest']).write_bytes(b'partial')
        with mock.patch.object(local_export, 'execute', side_effect=transfer) as run:
            with self.assertRaises(tarfile.ReadError):
                local_export.extract(self.graph, 'extract', self.root, ['docker'])
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.root / 'files-attempt2').exists())

    def test_symlink_chain_cannot_escape_after_all_links_exist(self):
        directory = tarfile.TarInfo('outer')
        directory.type = tarfile.DIRTYPE
        first = tarfile.TarInfo('outer/sub')
        first.type, first.linkname = tarfile.SYMTYPE, '../..'
        second = tarfile.TarInfo('escape')
        second.type, second.linkname = tarfile.SYMTYPE, 'outer/sub/../outside'
        path = self.root / 'chain.tar'
        self.archive(path, [(directory, None), (first, None), (second, None)])
        destination = self.root / 'chain'
        destination.mkdir()
        with self.assertRaises((IdentityError, tarfile.TarError)):
            local_export.unpack(path, destination)
        self.assertFalse((self.root / 'outside').exists())

    def test_existing_archive_cannot_be_overwritten(self):
        (self.root / 'export.tar').symlink_to(self.root / 'outside')
        with mock.patch.object(local_export, 'execute') as run:
            with self.assertRaises(IdentityError):
                local_export.extract(self.graph, 'extract', self.root, ['docker'])
        run.assert_not_called()

    def test_second_timeout_fails_with_both_attempts_preserved(self):
        with mock.patch.object(local_export, "execute", side_effect=subprocess.TimeoutExpired([], 600)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                local_export.extract(self.graph, "extract", self.root, ["docker"])
        self.assertEqual(run.call_count, 2)
        for attempt in (1, 2):
            self.assertEqual(json.loads((self.root / ("export-timeout-%d.json" % attempt)).read_text())["status"], "timed-out")

    def test_failed_export_is_not_retried(self):
        with mock.patch.object(local_export, "execute", side_effect=subprocess.CalledProcessError(1, [])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                local_export.extract(self.graph, "extract", self.root, ["docker"])
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.root / "files-attempt2").exists())

    def test_existing_output_cannot_be_consumed_or_overwritten(self):
        (self.root / "files").symlink_to(self.root, target_is_directory=True)
        with mock.patch.object(local_export, "execute") as run:
            with self.assertRaises(FileExistsError):
                local_export.extract(self.graph, "extract", self.root, ["docker"])
        run.assert_not_called()

    def test_unpinned_input_and_execution_instructions_are_rejected(self):
        for change in ("unpinned", "run", "remote", "output"):
            with self.subTest(change=change):
                graph = copy.deepcopy(self.graph)
                target = graph["target"]["extract"]
                if change == "unpinned":
                    target["contexts"]["artifact"] = "oci-layout:///input"
                elif change == "remote":
                    target["contexts"]["artifact"] = "docker-image://example/image@sha256:" + "a" * 64
                elif change == "run":
                    target["dockerfile-inline"] += "RUN qualify\n"
                else:
                    target["output"][0]["type"] = "registry"
                with mock.patch.object(local_export, "execute") as run:
                    with self.assertRaises(IdentityError):
                        local_export.extract(graph, "extract", self.root, ["docker"])
                    run.assert_not_called()

    def test_success_and_nonzero_status_are_preserved(self):
        local_export.execute([sys.executable, "-c", "pass"], self.root, timeout=5)
        with self.assertRaises(subprocess.CalledProcessError) as error:
            local_export.execute([sys.executable, "-c", "raise SystemExit(7)"], self.root, timeout=5)
        self.assertEqual(error.exception.returncode, 7)

    def test_export_diagnostics_survive_sdk_staging_cleanup(self):
        from crossforge_internal import ci_python_rows, ci_sdk
        data, diagnostics = self.root / "data", self.root / "diagnostics"
        (data / "extracted/files").mkdir(parents=True)
        (data / "extracted/files/partial").write_text("incomplete installed tree")
        (data / "extracted/export-timeout-1.json").write_text('{"status":"timed-out"}')
        (data / "extracted/extract.bake.json").write_text(json.dumps(self.graph))
        ci_python_rows.preserve_qualification(data, diagnostics, "cp314")
        ci_sdk.discard_row_intermediates(data)
        self.assertFalse((data / "extracted").exists())
        self.assertEqual(json.loads((diagnostics / "extracted/export-timeout-1.json").read_text())["status"], "timed-out")
        self.assertEqual(json.loads((diagnostics / "extracted/extract.bake.json").read_text()), self.graph)
        self.assertFalse((diagnostics / "extracted/files").exists())

    def test_timeout_stops_child_even_when_docker_parent_exits_first(self):
        script = self.root / "children.py"
        marker = self.root / "child-pid"
        script.write_text("""import os, pathlib, signal, sys, time
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
    while True:
        time.sleep(1)
while True:
    time.sleep(1)
""")
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                local_export.execute([sys.executable, str(script), str(marker)], self.root, timeout=1)
            self.assertTrue(marker.is_file(), "child must have started before testing its cleanup")
            child = int(marker.read_text())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = Path("/proc/%d/stat" % child)
                if not state.exists() or state.read_text().split()[2] == "Z":
                    break
                time.sleep(0.01)
            else:
                self.fail("timed-out export left its child running")
        finally:
            if marker.exists():
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
