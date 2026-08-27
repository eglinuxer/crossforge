//! Cross CPython build pipeline — "python packs" (design doc §9, milestone M6).
//!
//! Builds relocatable CPython installations with a crossforge toolchain, as
//! the arch-specific material for cross wheel building: the target
//! `pyconfig.h` + `_sysconfigdata_*.py` plus an interpreter for import smoke
//! tests. Per version the x86_64 build comes first (native under the el8
//! build container; it doubles as the build-python), then each cross arch.
//!
//! Configure options track the manylinux_2_28 image builds
//! (`--disable-shared --with-ensurepip=no`, prefix
//! `/opt/_internal/cpython-<version>`) so pyconfig.h / sysconfigdata can be
//! diffed against the official images as a supply-chain cross-check.
//!
//! Cross mechanics per version family:
//! - 3.11+: official `--with-build-python=<native python>`.
//! - 3.9/3.10: no such option; configure requires a `python3.X` of the same
//!   version on PATH (used as PYTHON_FOR_BUILD), and setup.py derives target
//!   include/library dirs from the sysrooted cross `$CC -E -v` output.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::compiler::ToolchainSources;
use crate::engine::{Cmd, Runner};
use crate::error::{Error, Result};
use crate::fetch::Fetcher;
use crate::target::TargetArch;

/// CPython versions built by default: the newest release of every supported
/// minor version (registered in `toolchain-sources.toml`).
pub const PYTHON_VERSIONS: &[&str] = &["3.9.25", "3.10.21", "3.11.16", "3.12.14", "3.13.15"];

/// The only baseline python packs are built for: wheels target
/// manylinux_2_28 exclusively (design doc §9, T2).
pub const PYTHON_BASELINE: &str = "el8";

/// Modules every pack must import successfully in the smoke test. Covers each
/// external target dependency (zlib, bzip2, xz, libffi, openssl, sqlite,
/// libuuid) plus core built-ins.
const SMOKE_IMPORTS: &[&str] = &[
    "math", "struct", "json", "zlib", "bz2", "lzma", "ctypes", "ssl", "hashlib", "sqlite3", "uuid",
];

/// Relative directory inside a pack holding crossforge metadata.
pub const META_DIR: &str = ".crossforge";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PythonPackMetadata {
    pub version: String,
    pub arch: String,
    pub triple: String,
    /// Configured installation prefix (the manylinux-style absolute path the
    /// tree was configured for; the pack itself is relocatable).
    pub prefix: String,
    pub configure_args: Vec<String>,
    pub generator: String,
}

/// A built python pack on disk.
#[derive(Debug, Clone)]
pub struct PythonPack {
    /// Pack root (the DESTDIR the install went into).
    pub root: PathBuf,
    /// The installed prefix inside the pack, e.g.
    /// `<root>/opt/_internal/cpython-3.12.14`.
    pub prefix: PathBuf,
    pub version: String,
    pub arch: TargetArch,
}

impl PythonPack {
    /// The interpreter binary, e.g. `<prefix>/bin/python3.12`.
    pub fn python_bin(&self) -> PathBuf {
        self.prefix
            .join("bin")
            .join(format!("python{}", minor_version(&self.version)))
    }

    /// Opens an already-built pack under `out_root` (the `python-packs`
    /// directory), erroring if it has not been built yet.
    pub fn open(out_root: &Path, version: &str, arch: TargetArch) -> Result<Self> {
        let install_prefix = format!("opt/_internal/cpython-{version}");
        let root = out_root.join(format!("{}-{arch}", pack_tag(version)));
        let pack = Self {
            prefix: root.join(install_prefix),
            root,
            version: version.to_string(),
            arch,
        };
        if !pack.python_bin().is_file() {
            return Err(Error::PythonPack(format!(
                "pack for cpython {version} ({arch}) not found under {} (run `crossforge python` first)",
                pack.root.display()
            )));
        }
        Ok(pack)
    }
}

/// Short pack tag for a full version: `3.12.14` → `cp312`.
pub fn pack_tag(version: &str) -> String {
    format!("cp{}", minor_version(version).replace('.', ""))
}

/// Minor version prefix: `3.12.14` → `3.12`.
fn minor_version(version: &str) -> &str {
    match version.match_indices('.').nth(1) {
        Some((idx, _)) => &version[..idx],
        None => version,
    }
}

/// Whether this version supports `--with-build-python` (3.11+).
fn has_build_python_option(version: &str) -> bool {
    !matches!(minor_version(version), "3.9" | "3.10")
}

/// Pure computation of the configure argument list (unit-testable).
fn configure_args(
    version: &str,
    arch: TargetArch,
    install_prefix: &str,
    build_python: Option<&Path>,
) -> Vec<String> {
    let mut args = vec![
        format!("--prefix={install_prefix}"),
        "--disable-shared".to_string(),
        "--with-ensurepip=no".to_string(),
    ];
    if arch != TargetArch::X86_64 {
        args.push("--build=x86_64-pc-linux-gnu".to_string());
        args.push(format!("--host={}", arch.triple()));
        // Cross-mode configure cannot probe the target; these are the answers
        // for any Linux target (configure requires the /dev/ptmx + /dev/ptc
        // pair as explicit input in cross builds; /dev/ptc is AIX-only).
        args.push("ac_cv_file__dev_ptmx=yes".to_string());
        args.push("ac_cv_file__dev_ptc=no".to_string());
        args.push("ac_cv_buggy_getaddrinfo=no".to_string());
        // Run-test probes that default to the pessimistic answer under cross
        // compilation; the real answers for any glibc target (verified
        // against native builds — the official manylinux images).  Without
        // ac_cv_computed_gotos the eval loop falls back to the slow switch
        // dispatch; the others cost multiprocessing/time features.
        args.push("ac_cv_computed_gotos=yes".to_string());
        args.push("ac_cv_aligned_required=no".to_string());
        args.push("ac_cv_broken_sem_getvalue=no".to_string());
        args.push("ac_cv_working_tzset=yes".to_string());
        if has_build_python_option(version) {
            if let Some(bp) = build_python {
                args.push(format!("--with-build-python={}", bp.display()));
            }
        }
    }
    args
}

/// Builds python packs with a crossforge toolchain.
#[derive(Debug)]
pub struct PythonBuilder<'a, R: Runner> {
    pub fetcher: &'a Fetcher,
    pub runner: &'a R,
    pub sources: ToolchainSources,
    /// Directory for source trees, build trees and logs.
    pub work_dir: PathBuf,
    pub jobs: usize,
}

impl<'a, R: Runner> PythonBuilder<'a, R> {
    /// Builds CPython `version` for `arch` with the toolchain at
    /// `toolchain_prefix` (which must target `arch`), installing under
    /// `out_root/<tag>-<arch>`. Cross builds (`arch != x86_64`) require the
    /// matching native pack as `build_python`.
    ///
    /// Idempotent: an existing installed interpreter short-circuits.
    pub fn build(
        &self,
        version: &str,
        arch: TargetArch,
        toolchain_prefix: &Path,
        build_python: Option<&PythonPack>,
        out_root: &Path,
    ) -> Result<PythonPack> {
        let component = self.sources.get("cpython", version)?.clone();
        let tag = pack_tag(version);
        let install_prefix = format!("/opt/_internal/cpython-{version}");
        let pack_root = out_root.join(format!("{tag}-{arch}"));
        let pack = PythonPack {
            prefix: pack_root.join(&install_prefix[1..]),
            root: pack_root,
            version: version.to_string(),
            arch,
        };
        if pack.python_bin().is_file() {
            tracing::info!(root = %pack.root.display(), "python pack already built, skipping");
            return Ok(pack);
        }
        let cross = arch != TargetArch::X86_64;
        if cross && build_python.is_none() {
            return Err(Error::PythonPack(format!(
                "cross build for {arch} requires the native x86_64 pack as build-python"
            )));
        }
        if let Some(bp) = build_python {
            if bp.version != version {
                return Err(Error::PythonPack(format!(
                    "build-python version {} does not match target version {version}",
                    bp.version
                )));
            }
        }

        let triple = arch.triple();
        let sysroot = toolchain_prefix.join(&triple).join("sysroot");
        if !sysroot.is_dir() {
            return Err(Error::PythonPack(format!(
                "toolchain sysroot not found: {}",
                sysroot.display()
            )));
        }
        let logs = self
            .work_dir
            .join("logs")
            .join(format!("python-{tag}-{arch}"));
        std::fs::create_dir_all(&logs)?;

        // 1. Fetch and unpack the source (shared across arches; the build
        // itself is out-of-tree).
        let src_root = self.work_dir.join("src");
        std::fs::create_dir_all(&src_root)?;
        let src_dir = src_root.join(format!("Python-{version}"));
        if !src_dir.is_dir() {
            let tarball = self
                .fetcher
                .fetch_cached(&component.url(), &component.sha256)?;
            tracing::info!(version, "unpacking cpython");
            self.runner.exec(
                &Cmd::new("tar")
                    .args([
                        "-xf",
                        &tarball.display().to_string(),
                        "-C",
                        &src_root.display().to_string(),
                    ])
                    .log(logs.join("unpack.log")),
            )?;
            if !src_dir.is_dir() {
                return Err(Error::PythonPack(format!(
                    "tarball did not produce {}",
                    src_dir.display()
                )));
            }
        }

        // 2. Configure. Build dirs are never reused across runs (stale
        // config.cache / Makefile state from another toolchain would leak).
        let build_dir = self
            .work_dir
            .join("python")
            .join(format!("build-{tag}-{arch}"));
        if build_dir.exists() {
            std::fs::remove_dir_all(&build_dir)?;
        }
        std::fs::create_dir_all(&build_dir)?;

        // PATH: toolchain first ($host-gcc etc.); for 3.9/3.10 cross builds
        // the native pack's bin dir supplies the `python3.X` that configure
        // picks as PYTHON_FOR_BUILD.
        let mut path_env = format!("{}/bin", toolchain_prefix.display());
        if let Some(bp) = build_python {
            path_env.push_str(&format!(":{}/bin", bp.prefix.display()));
        }
        path_env.push_str(":/usr/local/bin:/usr/bin:/bin");
        // Target library detection (openssl, sqlite, ...) resolves against
        // the toolchain sysroot, never the build host.
        let pkg_config_libdir = format!(
            "{sr}/usr/lib64/pkgconfig:{sr}/usr/share/pkgconfig",
            sr = sysroot.display()
        );

        let args = configure_args(
            version,
            arch,
            &install_prefix,
            build_python.map(|bp| bp.python_bin()).as_deref(),
        );
        tracing::info!(version, arch = %arch, cross, "configuring cpython");
        self.runner.exec(
            &Cmd::new(src_dir.join("configure").display().to_string())
                .args(args.clone())
                .cwd(&build_dir)
                .env("PATH", &path_env)
                .env("CC", format!("{triple}-gcc"))
                .env("CXX", format!("{triple}-g++"))
                .env("AR", format!("{triple}-ar"))
                .env("RANLIB", format!("{triple}-ranlib"))
                .env("READELF", format!("{triple}-readelf"))
                .env("PKG_CONFIG_LIBDIR", &pkg_config_libdir)
                .env("PKG_CONFIG_SYSROOT_DIR", sysroot.display().to_string())
                .log(logs.join("configure.log")),
        )?;

        // 3. Build + install into the pack (DESTDIR keeps the configured
        // manylinux-style prefix out of the filesystem).
        tracing::info!(version, arch = %arch, "building cpython");
        self.runner.exec(
            &Cmd::new("make")
                .arg(format!("-j{}", self.jobs))
                .cwd(&build_dir)
                .env("PATH", &path_env)
                .log(logs.join("make.log")),
        )?;
        self.runner.exec(
            &Cmd::new("make")
                .args(["install", &format!("DESTDIR={}", pack.root.display())])
                .cwd(&build_dir)
                .env("PATH", &path_env)
                .log(logs.join("install.log")),
        )?;
        if !pack.python_bin().is_file() {
            return Err(Error::PythonPack(format!(
                "install did not produce {}",
                pack.python_bin().display()
            )));
        }

        // 3b. Production trim, mirroring the official manylinux images: strip
        // the interpreter and extension modules, drop the embedding-only
        // static libpython and the test suite (wheel building needs neither).
        let minor = minor_version(version);
        let strip = format!("{triple}-strip");
        self.runner.exec(
            &Cmd::new(&strip)
                .arg(pack.python_bin().display().to_string())
                .env("PATH", &path_env)
                .log(logs.join("trim.log")),
        )?;
        let dynload = pack
            .prefix
            .join("lib")
            .join(format!("python{minor}"))
            .join("lib-dynload");
        let mut so_files = Vec::new();
        if dynload.is_dir() {
            for entry in std::fs::read_dir(&dynload)? {
                let path = entry?.path();
                if path.extension().is_some_and(|e| e == "so") {
                    so_files.push(path.display().to_string());
                }
            }
        }
        if !so_files.is_empty() {
            self.runner.exec(
                &Cmd::new(&strip)
                    .arg("--strip-unneeded")
                    .args(so_files)
                    .env("PATH", &path_env)
                    .log(logs.join("trim.log")),
            )?;
        }
        // The static library is installed twice: lib/ and the per-platform
        // lib/pythonX.Y/config-X.Y-<multiarch>/ directory.
        let lib_dir = pack.prefix.join("lib");
        let static_lib = lib_dir.join(format!("libpython{minor}.a"));
        if static_lib.is_file() {
            std::fs::remove_file(&static_lib)?;
        }
        for entry in std::fs::read_dir(lib_dir.join(format!("python{minor}")))? {
            let path = entry?.path();
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            if name.starts_with("config-") && path.is_dir() {
                let nested = path.join(format!("libpython{minor}.a"));
                if nested.is_file() {
                    std::fs::remove_file(&nested)?;
                }
            }
        }
        let test_dir = pack
            .prefix
            .join("lib")
            .join(format!("python{minor}"))
            .join("test");
        if test_dir.is_dir() {
            std::fs::remove_dir_all(&test_dir)?;
        }

        // 4. Metadata.
        let metadata = PythonPackMetadata {
            version: version.to_string(),
            arch: arch.to_string(),
            triple,
            prefix: install_prefix,
            configure_args: args,
            generator: format!("crossforge {}", env!("CARGO_PKG_VERSION")),
        };
        let meta_dir = pack.root.join(META_DIR);
        std::fs::create_dir_all(&meta_dir)?;
        std::fs::write(
            meta_dir.join("python.toml"),
            toml::to_string_pretty(&metadata)?,
        )?;
        tracing::info!(root = %pack.root.display(), "python pack built");
        Ok(pack)
    }

    /// Import smoke test: every module in the required list must import.
    /// x86_64 packs run directly (inside the build container); other arches
    /// run under user-mode qemu against the toolchain sysroot.
    pub fn smoke(&self, pack: &PythonPack, toolchain_prefix: &Path) -> Result<()> {
        let logs = self.work_dir.join("logs").join(format!(
            "python-{}-{}",
            pack_tag(&pack.version),
            pack.arch
        ));
        let program = format!("import {}; print('smoke-ok')", SMOKE_IMPORTS.join(", "));
        let python = pack.python_bin();
        let cmd = match pack.arch {
            TargetArch::X86_64 => Cmd::new(python.display().to_string()),
            _ => {
                let sysroot = toolchain_prefix.join(pack.arch.triple()).join("sysroot");
                Cmd::new(format!("qemu-{}", pack.arch.as_str()))
                    .arg("-L")
                    .arg(sysroot.display().to_string())
                    .arg(python.display().to_string())
            }
        };
        tracing::info!(pack = %pack.root.display(), "import smoke test");
        self.runner
            .exec(&cmd.args(["-c", &program]).log(logs.join("smoke.log")))
            .map_err(|e| {
                Error::PythonPack(format!(
                    "import smoke failed for {} ({}): {e} (imports: {})",
                    pack.version,
                    pack.arch,
                    SMOKE_IMPORTS.join(", ")
                ))
            })?;
        tracing::info!(version = %pack.version, arch = %pack.arch, "import smoke passed");
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pack_tags_and_minor_versions() {
        assert_eq!(pack_tag("3.9.25"), "cp39");
        assert_eq!(pack_tag("3.13.15"), "cp313");
        assert_eq!(minor_version("3.10.21"), "3.10");
        assert!(!has_build_python_option("3.10.21"));
        assert!(has_build_python_option("3.11.16"));
    }

    #[test]
    fn builtin_sources_cover_all_python_versions() {
        let sources = ToolchainSources::builtin();
        for version in PYTHON_VERSIONS {
            let c = sources.get("cpython", version).unwrap();
            assert_eq!(c.sha256.len(), 64);
            assert!(c.url().contains(version));
        }
    }

    #[test]
    fn native_configure_args_track_manylinux() {
        let args = configure_args("3.12.14", TargetArch::X86_64, "/opt/_internal/x", None);
        assert!(args.contains(&"--disable-shared".to_string()));
        assert!(args.contains(&"--with-ensurepip=no".to_string()));
        assert!(!args.iter().any(|a| a.starts_with("--host")));
    }

    #[test]
    fn cross_configure_args_use_build_python_only_on_311_plus() {
        let bp = PathBuf::from("/packs/cp39-x86_64/opt/x/bin/python3.9");
        let args = configure_args("3.9.25", TargetArch::Aarch64, "/opt/x", Some(&bp));
        assert!(args.contains(&"--host=aarch64-unknown-linux-gnu".to_string()));
        assert!(args.contains(&"ac_cv_file__dev_ptmx=yes".to_string()));
        assert!(!args.iter().any(|a| a.starts_with("--with-build-python")));
        let args = configure_args("3.12.14", TargetArch::Aarch64, "/opt/x", Some(&bp));
        assert!(args.iter().any(|a| a.starts_with("--with-build-python=")));
    }
}
