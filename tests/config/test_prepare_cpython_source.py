import copy
import io
import json
import runpy
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
PREPARER = runpy.run_path(str(REPOSITORY / "scripts/prepare-cpython-source.py"))
PreparationError = PREPARER["PreparationError"]
ROW_CONTRACT = runpy.run_path(str(REPOSITORY / "scripts/python_row_contract.py"))


FILEFINDER_SYSCONFIG = '''import os
def _init_posix(vars):
    name = _get_sysconfigdata_name()
    if (path := os.environ.get('_PYTHON_SYSCONFIGDATA_PATH')):
        from importlib.machinery import FileFinder, SourceFileLoader, SOURCE_SUFFIXES
        from importlib.util import module_from_spec
        spec = FileFinder(path, (SourceFileLoader, SOURCE_SUFFIXES)).find_spec(name)
        _temp = module_from_spec(spec)
        spec.loader.exec_module(_temp)
    else:
        _temp = __import__(name, globals(), locals(), ['build_time_vars'], 0)
    vars.update(_temp.build_time_vars)

def _init_non_posix(vars):
    pass
'''


PATHFINDER_SYSCONFIG = '''import os
import sys

def _import_from_directory(path, name):
    if name not in sys.modules:
        import importlib.machinery
        import importlib.util

        spec = importlib.machinery.PathFinder.find_spec(name, [path])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]

def _get_sysconfigdata_name():
    return '_sysconfigdata_test'

def _get_sysconfigdata():
    import importlib

    name = _get_sysconfigdata_name()
    path = os.environ.get('_PYTHON_SYSCONFIGDATA_PATH')
    module = _import_from_directory(path, name) if path else importlib.import_module(name)

    return module.build_time_vars

def _init_posix(vars):
    """Initialize the module as appropriate for POSIX systems."""
    vars.update(_get_sysconfigdata() | vars)

def _init_non_posix(vars):
    pass
'''


DISTUTILS_SYSCONFIG_DELEGATION = '''from sysconfig import _init_posix as sysconfig_init_posix

_config_vars = None

def _init_posix():
    """Initialize POSIX target configuration."""
    global _config_vars
    config_vars = {}
    sysconfig_init_posix(config_vars)
    _config_vars = config_vars

def _init_nt():
    pass
'''


class PrepareCPythonSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )

    def test_only_qualified_rows_are_implemented(self):
        for contract in ROW_CONTRACT["IMPLEMENTED_ROWS"]:
            with self.subTest(row=contract["row"]):
                entry = PREPARER["row_for"](self.config, contract["row"])
                self.assertEqual(
                    entry["version"].rsplit(".", 1)[0], contract["minor"]
                )
                self.assertEqual(entry["adapter"], contract["adapter"])

    def test_row_adapter_mismatch_is_rejected(self):
        config = copy.deepcopy(self.config)
        transition = next(
            entry
            for entry in config["python"]["versions"]
            if entry["version"].rsplit(".", 1)[0] == "3.11"
        )
        transition["adapter"] = "modern"
        with self.assertRaises(PreparationError):
            PREPARER["row_for"](config, "cp311")

    def test_prepare_is_atomic_and_writes_row_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "Python-3.11.16.tar.xz"
            self.write_archive(archive)
            config = self.configuration_for(archive)
            destination = directory / "source"
            manifest = directory / "manifest.json"
            identity = PREPARER["prepare"](
                config, "cp311", archive, destination, manifest, REPOSITORY
            )
            self.assertEqual(identity["compact"], "311")
            self.assertEqual(identity["patches"], [])
            self.assertEqual(identity["support"], "security")
            self.assertEqual(
                identity["release_sha256"], PREPARER["canonical_sha256"](config)
            )
            self.assertTrue((destination / "configure").is_file())
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), identity)
            with self.assertRaises(PreparationError):
                PREPARER["prepare"](
                    config, "cp311", archive, destination, manifest, REPOSITORY
                )

    def test_manifest_publish_failure_rolls_back_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "Python-3.11.16.tar.xz"
            self.write_archive(archive)
            config = self.configuration_for(archive)
            destination = directory / "source"
            manifest = directory / "manifest.json"
            with mock.patch.object(
                PREPARER["os"], "replace", side_effect=OSError("publish failed")
            ):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    PREPARER["prepare"](
                        config,
                        "cp311",
                        archive,
                        destination,
                        manifest,
                        REPOSITORY,
                    )
            self.assertFalse(destination.exists())
            self.assertFalse(manifest.exists())
            self.assertEqual(list(directory.glob(".*.tmp")), [])

    def test_cp312_patch_applies_and_manifest_binds_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "Python-3.12.14.tar.xz"
            self.write_archive(archive, version="3.12.14", vulnerable=True)
            patches = copy.deepcopy(
                PREPARER["row_for"](self.config, "cp312")["patches"]
            )
            config = self.configuration_for(
                archive,
                version="3.12.14",
                adapter="modern",
                patches=patches,
            )
            destination = directory / "source"
            manifest = directory / "manifest.json"
            identity = PREPARER["prepare"](
                config, "cp312", archive, destination, manifest, REPOSITORY
            )

            self.assertEqual(identity["row"], "cp312")
            self.assertEqual(identity["version"], "3.12.14")
            self.assertEqual(identity["adapter"], "modern")
            self.assertEqual(identity["patches"], patches)
            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8")), identity
            )
            for relative in ("configure", "configure.ac"):
                text = (destination / relative).read_text(encoding="utf-8")
                self.assertIn("PYTHONPATH=$(srcdir)/Lib", text)
                self.assertIn("_PYTHON_SYSCONFIGDATA_PATH=", text)
                self.assertNotIn(
                    "$(abs_builddir)/`cat pybuilddir.txt`:)$(srcdir)/Lib",
                    text,
                )
            sysconfig = (destination / "Lib/sysconfig.py").read_text(
                encoding="utf-8"
            )
            self.assertIn("os.environ.get('_PYTHON_SYSCONFIGDATA_PATH')", sysconfig)
            self.assertIn("FileFinder", sysconfig)

    def test_cp312_unpatched_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "Python-3.12.14.tar.xz"
            self.write_archive(archive, version="3.12.14", vulnerable=True)
            config = self.configuration_for(
                archive, version="3.12.14", adapter="modern", patches=[]
            )
            destination = directory / "source"
            manifest = directory / "manifest.json"
            with self.assertRaisesRegex(PreparationError, "lacks isolated"):
                PREPARER["prepare"](
                    config, "cp312", archive, destination, manifest, REPOSITORY
                )
            self.assertFalse(destination.exists())
            self.assertFalse(manifest.exists())

    def test_cp312_patch_path_and_hash_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "Python-3.12.14.tar.xz"
            self.write_archive(archive, version="3.12.14", vulnerable=True)
            canonical = PREPARER["row_for"](self.config, "cp312")["patches"]
            mutations = (
                ("sha", "sha256", "0" * 64, "SHA256 mismatch"),
                (
                    "path",
                    "file",
                    "patches/cpython/3.12/missing.patch",
                    "missing CPython patch",
                ),
            )
            for name, field, value, message in mutations:
                with self.subTest(name=name):
                    patches = copy.deepcopy(canonical)
                    patches[0][field] = value
                    config = self.configuration_for(
                        archive,
                        version="3.12.14",
                        adapter="modern",
                        patches=patches,
                    )
                    with self.assertRaisesRegex(PreparationError, message):
                        PREPARER["prepare"](
                            config,
                            "cp312",
                            archive,
                            directory / ("source-" + name),
                            directory / ("manifest-" + name + ".json"),
                            REPOSITORY,
                        )

    def test_patch_parent_symlink_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            repository = directory / "repository"
            patch_parent = repository / "patches/cpython"
            patch_parent.mkdir(parents=True)
            outside = directory / "outside"
            outside.mkdir()
            payload = outside / "escape.patch"
            payload.write_text("not a repository patch\n", encoding="utf-8")
            (patch_parent / "3.12").symlink_to(outside, target_is_directory=True)
            digest, unused_size = PREPARER["sha256_file"](payload)
            with self.assertRaisesRegex(PreparationError, "escapes repository"):
                PREPARER["patch_path"](
                    repository,
                    {
                        "file": "patches/cpython/3.12/escape.patch",
                        "sha256": digest,
                    },
                )

    def test_cp313_package_layout_keeps_filefinder_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "Python-3.13.15"
            self.write_isolation_tree(source, FILEFINDER_SYSCONFIG)
            PREPARER["validate_sysconfig_isolation"](source, "3.13.15")

    def test_cp39_distutils_delegates_atomically_to_isolated_sysconfig(self):
        mutations = {
            "valid": DISTUTILS_SYSCONFIG_DELEGATION,
            "missing alias": DISTUTILS_SYSCONFIG_DELEGATION.replace(
                "from sysconfig import _init_posix as sysconfig_init_posix",
                "from sysconfig import _init_posix",
            ),
            "dead-scope alias": DISTUTILS_SYSCONFIG_DELEGATION.replace(
                "from sysconfig import _init_posix as sysconfig_init_posix",
                "if False:\n"
                "    from sysconfig import _init_posix as sysconfig_init_posix",
            ),
            "ambient import": DISTUTILS_SYSCONFIG_DELEGATION.replace(
                "sysconfig_init_posix(config_vars)",
                "__import__('_sysconfigdata_target')",
            ),
            "published before load": DISTUTILS_SYSCONFIG_DELEGATION.replace(
                "sysconfig_init_posix(config_vars)\n    _config_vars = config_vars",
                "_config_vars = config_vars\n    sysconfig_init_posix(config_vars)",
            ),
            "delegates global": DISTUTILS_SYSCONFIG_DELEGATION.replace(
                "config_vars = {}\n    sysconfig_init_posix(config_vars)\n"
                "    _config_vars = config_vars",
                "_config_vars = {}\n    sysconfig_init_posix(_config_vars)",
            ),
            "missing publication": DISTUTILS_SYSCONFIG_DELEGATION.replace(
                "    _config_vars = config_vars\n", ""
            ),
            "duplicate delegation": DISTUTILS_SYSCONFIG_DELEGATION.replace(
                "sysconfig_init_posix(config_vars)",
                "sysconfig_init_posix(config_vars)\n"
                "    sysconfig_init_posix(config_vars)",
            ),
        }
        for name, distutils_sysconfig in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "Python-3.9.25"
                self.write_isolation_tree(source, FILEFINDER_SYSCONFIG)
                path = source / "Lib/distutils/sysconfig.py"
                path.parent.mkdir(parents=True)
                path.write_text(distutils_sysconfig, encoding="utf-8")
                if name == "valid":
                    PREPARER["validate_sysconfig_isolation"](
                        source, "3.9.25"
                    )
                else:
                    with self.assertRaises(PreparationError):
                        PREPARER["validate_sysconfig_isolation"](
                            source, "3.9.25"
                        )

    def test_cp314_pathfinder_sysconfig_isolation_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "Python-3.14.7"
            self.write_isolation_tree(source, PATHFINDER_SYSCONFIG)
            PREPARER["validate_sysconfig_isolation"](source, "3.14.7")

    def test_cp314_pathfinder_profile_rejects_missing_or_redirected_semantics(self):
        mutations = {
            "missing environment read": PATHFINDER_SYSCONFIG.replace(
                "path = os.environ.get('_PYTHON_SYSCONFIGDATA_PATH')",
                "path = None",
            ),
            "duplicate environment read": PATHFINDER_SYSCONFIG.replace(
                "path = os.environ.get('_PYTHON_SYSCONFIGDATA_PATH')",
                "path = os.environ.get('_PYTHON_SYSCONFIGDATA_PATH')\n"
                "    other = os.environ.get('_PYTHON_SYSCONFIGDATA_PATH')",
            ),
            "ambient search path": PATHFINDER_SYSCONFIG.replace(
                "PathFinder.find_spec(name, [path])",
                "PathFinder.find_spec(name, sys.path)",
            ),
            "reversed path branch": PATHFINDER_SYSCONFIG.replace(
                "if path else importlib.import_module(name)",
                "if not path else importlib.import_module(name)",
            ),
            "missing fallback import": PATHFINDER_SYSCONFIG.replace(
                "importlib.import_module(name)",
                "_import_from_directory(path, name)",
            ),
            "missing module factory": PATHFINDER_SYSCONFIG.replace(
                "module = importlib.util.module_from_spec(spec)",
                "module = object()",
            ),
            "missing module execution": PATHFINDER_SYSCONFIG.replace(
                "spec.loader.exec_module(module)",
                "pass",
            ),
            "missing module publication": PATHFINDER_SYSCONFIG.replace(
                "sys.modules[name] = module",
                "pass",
            ),
            "redirected module publication": PATHFINDER_SYSCONFIG.replace(
                "sys.modules[name] = module",
                "sys.modules['other'] = module",
            ),
            "missing cached module return": PATHFINDER_SYSCONFIG.replace(
                "return sys.modules[name]",
                "return module",
            ),
            "redirected cached module return": PATHFINDER_SYSCONFIG.replace(
                "return sys.modules[name]",
                "return sys.modules['other']",
            ),
            "initializer ignores target data": PATHFINDER_SYSCONFIG.replace(
                "vars.update(_get_sysconfigdata() | vars)",
                "vars.update(vars)",
            ),
        }
        for name, sysconfig in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "Python-3.14.7"
                self.write_isolation_tree(source, sysconfig)
                with self.assertRaises(PreparationError):
                    PREPARER["validate_sysconfig_isolation"](source, "3.14.7")

    def test_cp314_comment_and_docstring_decoys_do_not_satisfy_profile(self):
        deceptive = PATHFINDER_SYSCONFIG.replace(
            "        spec = importlib.machinery.PathFinder.find_spec(name, [path])\n"
            "        module = importlib.util.module_from_spec(spec)\n"
            "        spec.loader.exec_module(module)",
            "        # spec = importlib.machinery.PathFinder.find_spec(name, [path])\n"
            "        # module = importlib.util.module_from_spec(spec)\n"
            "        # spec.loader.exec_module(module)\n"
            "        details = \"\"\"\n"
            "        spec = importlib.machinery.PathFinder.find_spec(name, [path])\n"
            "        module = importlib.util.module_from_spec(spec)\n"
            "        spec.loader.exec_module(module)\n"
            "        \"\"\"\n"
            "        module = None",
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "Python-3.14.7"
            self.write_isolation_tree(source, deceptive)
            with self.assertRaises(PreparationError):
                PREPARER["validate_sysconfig_isolation"](source, "3.14.7")

    def test_archive_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "unsafe.tar.xz"
            with tarfile.open(str(archive), "w:xz") as output:
                root = tarfile.TarInfo("Python-3.11.16")
                root.type = tarfile.DIRTYPE
                output.addfile(root)
                link = tarfile.TarInfo("Python-3.11.16/configure")
                link.type = tarfile.SYMTYPE
                link.linkname = "/bin/true"
                output.addfile(link)
            extraction = Path(temporary) / "extract"
            extraction.mkdir()
            with self.assertRaises(PreparationError):
                PREPARER["extract_archive"](archive, extraction, "3.11.16")

    @staticmethod
    def write_archive(path, version="3.11.16", vulnerable=False):
        root_name = "Python-" + version
        if vulnerable:
            configure = b"""#!/bin/sh
# PYTHON_FOR_BUILD='PYTHONPATH=$(srcdir)/Lib _PYTHON_SYSCONFIGDATA_PATH=/comment-only'
    fi
        ac_cv_prog_PYTHON_FOR_REGEN=$with_build_python
    PYTHON_FOR_FREEZE="$with_build_python"
    PYTHON_FOR_BUILD='_PYTHON_PROJECT_BASE=$(abs_builddir) _PYTHON_HOST_PLATFORM=$(_PYTHON_HOST_PLATFORM) PYTHONPATH=$(shell test -f pybuilddir.txt && echo $(abs_builddir)/`cat pybuilddir.txt`:)$(srcdir)/Lib _PYTHON_SYSCONFIGDATA_NAME=_sysconfigdata_$(ABIFLAGS)_$(MACHDEP)_$(MULTIARCH) '$with_build_python
    { printf "%s\\n" "$as_me:${as_lineno-$LINENO}: result: $with_build_python" >&5
printf "%s\\n" "$with_build_python" >&6; }

"""
            configure_ac = b"""dnl PYTHON_FOR_BUILD='PYTHONPATH=$(srcdir)/Lib _PYTHON_SYSCONFIGDATA_PATH=/comment-only'
    dnl Build Python interpreter is used for regeneration and freezing.
    ac_cv_prog_PYTHON_FOR_REGEN=$with_build_python
    PYTHON_FOR_FREEZE="$with_build_python"
    PYTHON_FOR_BUILD='_PYTHON_PROJECT_BASE=$(abs_builddir) _PYTHON_HOST_PLATFORM=$(_PYTHON_HOST_PLATFORM) PYTHONPATH=$(shell test -f pybuilddir.txt && echo $(abs_builddir)/`cat pybuilddir.txt`:)$(srcdir)/Lib _PYTHON_SYSCONFIGDATA_NAME=_sysconfigdata_$(ABIFLAGS)_$(MACHDEP)_$(MULTIARCH) '$with_build_python
    AC_MSG_RESULT([$with_build_python])
  ], [
    AS_VAR_IF([cross_compiling], [yes],
"""
            sysconfig = b'''# os.environ.get('_PYTHON_SYSCONFIGDATA_PATH') FileFinder SourceFileLoader
def _init_posix(vars):
    """Initialize the module as appropriate for POSIX systems."""
    # _sysconfigdata is generated at build time, see _generate_posix_vars()
    name = _get_sysconfigdata_name()
    _temp = __import__(name, globals(), locals(), ['build_time_vars'], 0)
    build_time_vars = _temp.build_time_vars
    vars.update(build_time_vars)

def _init_non_posix(vars):
    pass

'''
        else:
            isolated = (
                b"PYTHON_FOR_BUILD='_PYTHON_PROJECT_BASE=$(abs_builddir) "
                b"PYTHONPATH=$(srcdir)/Lib "
                b"_PYTHON_SYSCONFIGDATA_PATH=$(abs_builddir)/target python'\n"
            )
            configure = b"#!/bin/sh\n" + isolated
            configure_ac = isolated
            sysconfig = b'''import os
def _init_posix(vars):
    name = _get_sysconfigdata_name()
    if (path := os.environ.get('_PYTHON_SYSCONFIGDATA_PATH')):
        from importlib.machinery import FileFinder, SourceFileLoader, SOURCE_SUFFIXES
        from importlib.util import module_from_spec
        spec = FileFinder(path, (SourceFileLoader, SOURCE_SUFFIXES)).find_spec(name)
        _temp = module_from_spec(spec)
        spec.loader.exec_module(_temp)
    else:
        _temp = __import__(name, globals(), locals(), ['build_time_vars'], 0)
    vars.update(_temp.build_time_vars)

def _init_non_posix(vars):
    pass
'''

        with tarfile.open(str(path), "w:xz") as output:
            for directory in (root_name, root_name + "/Lib"):
                member = tarfile.TarInfo(directory)
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
                output.addfile(member)
            for relative, payload, mode in (
                ("configure", configure, 0o755),
                ("configure.ac", configure_ac, 0o644),
                ("Lib/sysconfig.py", sysconfig, 0o644),
            ):
                member = tarfile.TarInfo(root_name + "/" + relative)
                member.mode = mode
                member.size = len(payload)
                output.addfile(member, io.BytesIO(payload))

    @staticmethod
    def write_isolation_tree(source, sysconfig):
        assignment = (
            "PYTHON_FOR_BUILD='_PYTHON_PROJECT_BASE=$(abs_builddir) "
            "_PYTHON_HOST_PLATFORM=$(_PYTHON_HOST_PLATFORM) "
            "PYTHONPATH=$(srcdir)/Lib "
            "_PYTHON_SYSCONFIGDATA_NAME=_sysconfigdata_$(ABIFLAGS)_$(MACHDEP)_$(MULTIARCH) "
            "_PYTHON_SYSCONFIGDATA_PATH=$(shell test -f pybuilddir.txt && echo "
            "$(abs_builddir)/`cat pybuilddir.txt`) '$with_build_python\n"
        )
        (source / "Lib/sysconfig").mkdir(parents=True)
        (source / "configure").write_text(assignment, encoding="utf-8")
        (source / "configure.ac").write_text(assignment, encoding="utf-8")
        (source / "Lib/sysconfig/__init__.py").write_text(
            sysconfig, encoding="utf-8"
        )

    @staticmethod
    def configuration_for(
        archive, version="3.11.16", adapter="transition", patches=None
    ):
        digest, size = PREPARER["sha256_file"](archive)
        if patches is None:
            patches = []
        return {
            "python": {
                "versions": [
                    {
                        "version": version,
                        "adapter": adapter,
                        "support": "security",
                        "source": {
                            "status": "locked",
                            "url": "https://example.invalid/Python-%s.tar.xz"
                            % version,
                            "sha256": digest,
                            "size": size,
                        },
                        "patches": patches,
                    }
                ]
            }
        }


if __name__ == "__main__":
    unittest.main()
