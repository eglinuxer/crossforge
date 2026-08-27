//! End-to-end wheel building (design doc §9, T5 / milestone M7): a cross
//! cibuildwheel. For one (project, CPython version, arch) this assembles a
//! build environment from the python packs, drives the project's PEP 517
//! backend through pip, then audits, retags and smoke-tests the result.
//!
//! The environment layer is what makes every backend cross-compile the same
//! way (conda-forge's proven recipe):
//! - a venv from the native x86_64 pack supplies the running interpreter
//!   (and pip, bootstrapped from a pinned wheel — packs ship without pip);
//! - `_PYTHON_SYSCONFIGDATA_NAME` + `_PYTHON_HOST_PLATFORM` + a PYTHONPATH
//!   holding only the target pack's `_sysconfigdata_*.py` make setuptools
//!   compute target CFLAGS/EXT_SUFFIX (the sysconfigdata CC already names
//!   the cross compiler);
//! - `CMAKE_TOOLCHAIN_FILE` + FindPython hints cover CMake backends
//!   (scikit-build-core / nanobind);
//! - `PYO3_CROSS_LIB_DIR` covers PyO3/maturin (interface prepared; sample
//!   deferred).
//!
//! Native x86_64 wheels go through the same pipeline minus the cross
//! variables — still with the crossforge toolchain, whose nonshared hybrid
//! linking is what keeps C++ wheels inside the policy's GLIBCXX ceiling.

use std::path::{Path, PathBuf};

use crate::audit::Severity;
use crate::compiler::ToolchainSources;
use crate::engine::{Cmd, Runner};
use crate::error::{Error, Result};
use crate::fetch::Fetcher;
use crate::python::PythonPack;
use crate::target::TargetArch;
use crate::wheelaudit::{self, WheelPolicy};
use crate::whl::{self, WheelName};

/// The pinned pip wheel used to bootstrap build environments (packs are
/// built `--with-ensurepip=no`, matching manylinux). 25.2 is the last line
/// supporting Python 3.9.
pub const PIP_VERSION: &str = "25.2";

/// One built, audited, retagged wheel.
#[derive(Debug, Clone)]
pub struct WheelArtifact {
    pub path: PathBuf,
    pub name: WheelName,
    pub python_version: String,
    pub arch: TargetArch,
    /// Hashed names of the libraries vendored into the wheel (empty when it
    /// was already policy-clean).
    pub vendored: Vec<String>,
}

impl WheelArtifact {
    /// abi3 wheels are validated against every interpreter version.
    pub fn is_abi3(&self) -> bool {
        self.name.abi_tag == "abi3"
    }
}

/// Everything one wheel build needs.
#[derive(Debug)]
pub struct WheelRequest<'a> {
    pub project_dir: &'a Path,
    /// Full CPython version, e.g. `3.12.14`.
    pub python_version: &'a str,
    pub arch: TargetArch,
    /// Toolchain prefix targeting `arch`.
    pub toolchain_prefix: &'a Path,
    /// Native x86_64 pack of the same version (the running interpreter).
    pub native_pack: &'a PythonPack,
    /// Pack for `arch` (equals `native_pack` for x86_64).
    pub target_pack: &'a PythonPack,
    pub out_dir: &'a Path,
}

/// Builds wheels through the python packs and a crossforge toolchain.
#[derive(Debug)]
pub struct WheelBuilder<'a, R: Runner> {
    pub fetcher: &'a Fetcher,
    pub runner: &'a R,
    pub sources: ToolchainSources,
    pub work_dir: PathBuf,
    pub policy: WheelPolicy,
    /// Extra directories to resolve vendorable target libraries from (the
    /// toolchain sysroot's lib dirs are always searched).
    pub vendor_paths: Vec<PathBuf>,
    /// Sonames the runtime provides (driver-style, e.g. `libcuda.so.1`):
    /// neither vendored nor flagged by the audit.
    pub exclude: Vec<String>,
}

impl<'a, R: Runner> WheelBuilder<'a, R> {
    /// Runs the full pipeline for one (version, arch): build env → pip wheel
    /// → policy audit → retag to the policy platform tag → move to out_dir.
    pub fn build(&self, req: &WheelRequest) -> Result<WheelArtifact> {
        let tag = crate::python::pack_tag(req.python_version);
        let id = format!("{tag}-{}", req.arch);
        let scratch = self.work_dir.join("wheel").join(&id);
        let logs = self.work_dir.join("logs").join(format!("wheel-{id}"));
        if scratch.exists() {
            std::fs::remove_dir_all(&scratch)?;
        }
        std::fs::create_dir_all(&scratch)?;
        std::fs::create_dir_all(&logs)?;
        std::fs::create_dir_all(req.out_dir)?;

        // 1. Build venv from the native pack and bootstrap pip.
        let pip_whl = self.fetch_pip()?;
        let venv = scratch.join("venv");
        let native_python = req.native_pack.python_bin();
        self.runner.exec(
            &Cmd::new(native_python.display().to_string())
                .args(["-m", "venv", "--without-pip", &venv.display().to_string()])
                .log(logs.join("venv.log")),
        )?;
        let venv_python = venv.join("bin").join("python");

        // 2. Install the project's build requirements (PEP 518) into the
        // venv; pip runs straight out of its own wheel via PYTHONPATH.
        let requires = build_requires(req.project_dir)?;
        tracing::info!(?requires, "installing build requirements");
        let home = scratch.join("home");
        std::fs::create_dir_all(&home)?;
        self.runner.exec(
            &Cmd::new(venv_python.display().to_string())
                .args(["-m", "pip", "install", "--quiet"])
                .args(requires.iter().map(String::as_str))
                .cwd(&scratch)
                .env("PYTHONPATH", pip_whl.display().to_string())
                .env("HOME", home.display().to_string())
                .env("PIP_DISABLE_PIP_VERSION_CHECK", "1")
                .log(logs.join("pip-install.log")),
        )?;

        // 3. Assemble the (cross) build environment and run pip wheel.
        let raw_dir = scratch.join("raw");
        std::fs::create_dir_all(&raw_dir)?;
        let triple = req.arch.triple();
        let path_env = format!(
            "{}/bin:{}/bin:/usr/local/bin:/usr/bin:/bin",
            req.toolchain_prefix.display(),
            venv.display()
        );
        let mut pythonpath = pip_whl.display().to_string();
        let mut wheel_cmd = Cmd::new(venv_python.display().to_string())
            .args([
                "-m",
                "pip",
                "wheel",
                &req.project_dir.display().to_string(),
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                &raw_dir.display().to_string(),
            ])
            .cwd(&scratch)
            .env("PATH", &path_env)
            .env("HOME", home.display().to_string())
            .env("PIP_DISABLE_PIP_VERSION_CHECK", "1")
            .env("CC", format!("{triple}-gcc"))
            .env("CXX", format!("{triple}-g++"))
            .env("AR", format!("{triple}-ar"))
            // cc-rs picks TARGET_CC for target objects and HOST_CC for build
            // scripts; without HOST_CC it would fall back to the cross CC
            // above and miscompile host-side build scripts.
            .env("TARGET_CC", format!("{triple}-gcc"))
            .env("TARGET_CXX", format!("{triple}-g++"))
            .env("TARGET_AR", format!("{triple}-ar"))
            .env("HOST_CC", "gcc")
            .env("HOST_CXX", "g++")
            // Cargo/maturin: build for the target triple and link with the
            // crossforge compiler. This also matters for native x86_64 —
            // the host gcc would produce binaries over the policy's glibc
            // ceiling.
            .env("CARGO_BUILD_TARGET", &triple)
            .env(
                format!("CARGO_TARGET_{}_LINKER", cargo_target_env(&triple)),
                format!("{triple}-gcc"),
            )
            .env(
                "CMAKE_TOOLCHAIN_FILE",
                req.toolchain_prefix
                    .join("toolchain.cmake")
                    .display()
                    .to_string(),
            )
            .log(logs.join("pip-wheel.log"));

        if req.arch != TargetArch::X86_64 {
            let (sysconfig_name, ext_suffix) = target_sysconfig(req.target_pack)?;
            // Only the target sysconfigdata goes on PYTHONPATH (plus the pip
            // wheel), never the whole target stdlib.
            let sysconfig_dir = scratch.join("target-sysconfig");
            std::fs::create_dir_all(&sysconfig_dir)?;
            let src = sysconfigdata_path(req.target_pack)?;
            std::fs::copy(&src, sysconfig_dir.join(src.file_name().unwrap()))?;
            let minor = pack_minor(req.target_pack);
            pythonpath = format!("{}:{pythonpath}", sysconfig_dir.display());
            wheel_cmd = wheel_cmd
                .env("_PYTHON_SYSCONFIGDATA_NAME", &sysconfig_name)
                .env(
                    "_PYTHON_HOST_PLATFORM",
                    format!("linux-{}", req.arch.as_str()),
                )
                .env("SETUPTOOLS_EXT_SUFFIX", &ext_suffix)
                .env(
                    "CMAKE_ARGS",
                    format!(
                        "-DPython_EXECUTABLE={} -DPython_INCLUDE_DIR={} -DPython_SOABI={}",
                        venv_python.display(),
                        req.target_pack
                            .prefix
                            .join("include")
                            .join(format!("python{minor}"))
                            .display(),
                        ext_suffix
                            .trim_start_matches('.')
                            .trim_end_matches(".so")
                            .trim_end_matches('.'),
                    ),
                )
                // PyO3 finds the target's sysconfigdata under this lib dir;
                // the version pin keeps it from guessing when several packs
                // share a search path.
                .env(
                    "PYO3_CROSS_LIB_DIR",
                    req.target_pack.prefix.join("lib").display().to_string(),
                )
                .env("PYO3_CROSS_PYTHON_VERSION", &minor);
        }
        wheel_cmd = wheel_cmd.env("PYTHONPATH", pythonpath);
        tracing::info!(project = %req.project_dir.display(), id, "building wheel");
        self.runner.exec(&wheel_cmd)?;

        // 4. Vendor non-whitelisted dependencies (no-op on clean wheels),
        // then audit and retag.
        let raw_wheel = single_wheel(&raw_dir)?;
        let file_name = raw_wheel
            .file_name()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        let parsed = WheelName::parse(&file_name)?;
        if parsed.platform_tag == "any" {
            return Err(Error::Wheel(format!(
                "{file_name}: pure-python wheel; crossforge wheel is for extension modules"
            )));
        }
        let sysroot = req.toolchain_prefix.join(&triple).join("sysroot");
        let mut search_paths = self.vendor_paths.clone();
        search_paths.push(sysroot.join("usr/lib64"));
        search_paths.push(sysroot.join("usr/lib"));
        let vendored = crate::vendor::vendor_wheel(
            &raw_wheel,
            req.arch,
            &self.policy,
            &search_paths,
            &self.exclude,
        )?;
        let report = wheelaudit::audit_wheel(&self.policy, &raw_wheel, req.arch, &self.exclude)?;
        for finding in &report.findings {
            let level = match finding.severity {
                Severity::Error => tracing::Level::ERROR,
                Severity::Warning => tracing::Level::WARN,
            };
            match level {
                tracing::Level::ERROR => {
                    tracing::error!(check = %finding.check, "{}", finding.message)
                }
                _ => tracing::warn!(check = %finding.check, "{}", finding.message),
            }
        }
        if !report.passed() {
            return Err(Error::Wheel(format!(
                "{file_name}: failed the {} policy audit ({} findings)",
                self.policy.policy,
                report.findings.len()
            )));
        }
        let retagged = whl::retag_platform(&raw_wheel, &self.policy.platform_tag(req.arch))?;
        let final_path = req.out_dir.join(retagged.file_name().unwrap());
        if final_path.exists() {
            std::fs::remove_file(&final_path)?;
        }
        std::fs::rename(&retagged, &final_path)?;
        let name = WheelName::parse(&final_path.file_name().unwrap().to_string_lossy())?;
        tracing::info!(wheel = %final_path.display(), "wheel built and audited");
        Ok(WheelArtifact {
            path: final_path,
            name,
            python_version: req.python_version.to_string(),
            arch: req.arch,
            vendored: vendored.into_iter().map(|v| v.vendored_name).collect(),
        })
    }

    /// Import smoke test: unpack the wheel and import every top-level module
    /// with each pack in `packs` (one pack normally; all five for abi3).
    /// x86_64 runs directly, other arches under qemu with the toolchain
    /// sysroot.
    pub fn smoke(
        &self,
        artifact: &WheelArtifact,
        packs: &[&PythonPack],
        toolchain_prefix: &Path,
    ) -> Result<()> {
        let site = self.work_dir.join("wheel").join(format!(
            "smoke-{}-{}",
            artifact.name.python_tag, artifact.arch
        ));
        if site.exists() {
            std::fs::remove_dir_all(&site)?;
        }
        std::fs::create_dir_all(&site)?;
        let entries = whl::read_wheel(&artifact.path)?;
        for entry in &entries {
            let dest = site.join(&entry.name);
            std::fs::create_dir_all(dest.parent().unwrap())?;
            std::fs::write(&dest, &entry.data)?;
        }
        let modules = top_level_modules(&entries);
        if modules.is_empty() {
            return Err(Error::Wheel(format!(
                "{}: no importable top-level modules found",
                artifact.path.display()
            )));
        }
        let program = format!("import {}; print('wheel-smoke-ok')", modules.join(", "));
        let logs = self.work_dir.join("logs").join(format!(
            "wheel-{}-{}",
            artifact.name.python_tag, artifact.arch
        ));
        for pack in packs {
            let python = pack.python_bin();
            let cmd = match artifact.arch {
                TargetArch::X86_64 => Cmd::new(python.display().to_string()),
                _ => {
                    let sysroot = toolchain_prefix
                        .join(artifact.arch.triple())
                        .join("sysroot");
                    Cmd::new(format!("qemu-{}", artifact.arch.as_str()))
                        .arg("-L")
                        .arg(sysroot.display().to_string())
                        .arg(python.display().to_string())
                }
            };
            self.runner
                .exec(
                    &cmd.args(["-c", &program])
                        .env("PYTHONPATH", site.display().to_string())
                        .log(logs.join(format!(
                            "smoke-{}.log",
                            crate::python::pack_tag(&pack.version)
                        ))),
                )
                .map_err(|e| {
                    Error::Wheel(format!(
                        "import smoke failed for {} on cpython {} ({}): {e}",
                        artifact.name.file_name(),
                        pack.version,
                        artifact.arch,
                    ))
                })?;
            tracing::info!(
                wheel = %artifact.name.file_name(),
                interpreter = %pack.version,
                "wheel import smoke passed"
            );
        }
        Ok(())
    }

    /// Convenience wrapper: install-layer check inside the official
    /// manylinux container for the wheel's arch.
    pub fn verify_in_manylinux(&self, artifact: &WheelArtifact, engine: &str) -> Result<()> {
        let image = format!("quay.io/pypa/manylinux_2_28_{}", artifact.arch.as_str());
        self.verify_in_images(artifact, &[image], engine)
    }

    /// Install-layer check (design doc §9, M8 `--verify-images`): imports the
    /// wheel's modules inside each container image with the image's own
    /// interpreter. The manylinux `/opt/python/<tag>` layout is preferred;
    /// otherwise `python3` is used when its version matches the wheel's tag,
    /// and non-matching images are skipped with a warning (distro sampling
    /// across a mixed image list stays usable).
    pub fn verify_in_images(
        &self,
        artifact: &WheelArtifact,
        images: &[String],
        engine: &str,
    ) -> Result<()> {
        let platform = match artifact.arch {
            TargetArch::X86_64 => "linux/amd64",
            _ => "linux/arm64",
        };
        let site = self.work_dir.join("wheel").join(format!(
            "smoke-{}-{}",
            artifact.name.python_tag, artifact.arch
        ));
        let entries = whl::read_wheel(&artifact.path)?;
        let modules = top_level_modules(&entries);
        let program = format!("import {}; print('install-verify-ok')", modules.join(", "));
        // For abi3 wheels this checks the tagged (minimum) interpreter; the
        // native/qemu smoke already fanned out across versions.
        let cp = &artifact.name.python_tag; // e.g. cp312
        let (major, minor) = cp
            .strip_prefix("cp")
            .and_then(|v| v.split_at_checked(1))
            .ok_or_else(|| Error::Wheel(format!("unsupported python tag {cp}")))?;
        // Resolve an interpreter, check its version, then import. Exit 42
        // signals "no matching interpreter" (skip, not failure).
        let script = format!(
            "P=/opt/python/{cp}-{cp}/bin/python; \
             if ! [ -x \"$P\" ]; then P=$(command -v python3) || exit 42; fi; \
             \"$P\" -c 'import sys; sys.exit(0 if sys.version_info[:2] == ({major}, {minor}) else 42)' || exit 42; \
             PYTHONPATH={site} exec \"$P\" -c \"{program}\"",
            site = site.display(),
        );
        for image in images {
            let log = self
                .work_dir
                .join("logs")
                .join(format!("wheel-{cp}-{}", artifact.arch))
                .join(format!("verify-{}.log", image.replace(['/', ':'], "_")));
            std::fs::create_dir_all(log.parent().unwrap())?;
            let status = std::process::Command::new(engine)
                .args(["run", "--rm", "--platform", platform])
                .args(["-v", &format!("{0}:{0}", site.display())])
                .arg(image)
                .args(["sh", "-c", &script])
                .output()?;
            std::fs::write(&log, [&status.stdout[..], &status.stderr[..]].concat())?;
            match status.status.code() {
                Some(0) => {
                    tracing::info!(wheel = %artifact.name.file_name(), image, "install verify passed");
                }
                Some(42) => {
                    tracing::warn!(
                        wheel = %artifact.name.file_name(),
                        image,
                        "skipped: no python {major}.{minor} interpreter in image"
                    );
                }
                _ => {
                    return Err(Error::Wheel(format!(
                        "install verify failed for {} in {image} (log: {})",
                        artifact.name.file_name(),
                        log.display()
                    )));
                }
            }
        }
        Ok(())
    }

    fn fetch_pip(&self) -> Result<PathBuf> {
        let component = self.sources.get("pip", PIP_VERSION)?;
        self.fetcher
            .fetch_cached(&component.url(), &component.sha256)
    }
}

/// PEP 518 build requirements; setuptools + wheel when no pyproject.toml.
fn build_requires(project_dir: &Path) -> Result<Vec<String>> {
    let pyproject = project_dir.join("pyproject.toml");
    if !pyproject.is_file() {
        return Ok(vec!["setuptools>=61".to_string(), "wheel".to_string()]);
    }
    let value: toml::Value = toml::from_str(&std::fs::read_to_string(&pyproject)?)?;
    let requires = value
        .get("build-system")
        .and_then(|b| b.get("requires"))
        .and_then(|r| r.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    if requires.is_empty() {
        Ok(vec!["setuptools>=61".to_string(), "wheel".to_string()])
    } else {
        Ok(requires)
    }
}

/// Whether `python_version` (full X.Y.Z) satisfies the project's
/// `[project].requires-python` lower bound (only `>=X.Y` specifiers are
/// interpreted; anything else builds unconditionally). This is how the
/// matrix skips interpreters a project does not support — e.g. nanobind
/// projects on 3.9.
pub fn project_supports_python(project_dir: &Path, python_version: &str) -> Result<bool> {
    let pyproject = project_dir.join("pyproject.toml");
    if !pyproject.is_file() {
        return Ok(true);
    }
    let value: toml::Value = toml::from_str(&std::fs::read_to_string(&pyproject)?)?;
    let Some(spec) = value
        .get("project")
        .and_then(|p| p.get("requires-python"))
        .and_then(|r| r.as_str())
    else {
        return Ok(true);
    };
    let Some(bound) = spec
        .split(',')
        .map(str::trim)
        .find_map(|s| s.strip_prefix(">="))
        .map(str::trim)
    else {
        return Ok(true);
    };
    let parse = |v: &str| -> Option<(u64, u64)> {
        let mut it = v.split('.');
        Some((it.next()?.parse().ok()?, it.next()?.parse().ok()?))
    };
    match (parse(bound), parse(python_version)) {
        (Some(min), Some(actual)) => Ok(actual >= min),
        _ => Ok(true),
    }
}

/// `aarch64-unknown-linux-gnu` → `AARCH64_UNKNOWN_LINUX_GNU`, the form
/// cargo uses in its per-target environment variables.
fn cargo_target_env(triple: &str) -> String {
    triple.to_uppercase().replace('-', "_")
}

fn pack_minor(pack: &PythonPack) -> String {
    let mut it = pack.version.split('.');
    format!("{}.{}", it.next().unwrap_or("3"), it.next().unwrap_or("0"))
}

fn sysconfigdata_path(pack: &PythonPack) -> Result<PathBuf> {
    let lib = pack
        .prefix
        .join("lib")
        .join(format!("python{}", pack_minor(pack)));
    for entry in std::fs::read_dir(&lib)? {
        let path = entry?.path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        if name.starts_with("_sysconfigdata_") && name.ends_with(".py") {
            return Ok(path);
        }
    }
    Err(Error::Wheel(format!(
        "no _sysconfigdata_*.py under {}",
        lib.display()
    )))
}

/// (module name of the sysconfigdata, EXT_SUFFIX) from the target pack.
fn target_sysconfig(pack: &PythonPack) -> Result<(String, String)> {
    let path = sysconfigdata_path(pack)?;
    let name = path.file_stem().unwrap().to_string_lossy().into_owned();
    let text = std::fs::read_to_string(&path)?;
    let ext_suffix = text
        .split("'EXT_SUFFIX': '")
        .nth(1)
        .and_then(|rest| rest.split('\'').next())
        .ok_or_else(|| Error::Wheel(format!("no EXT_SUFFIX in {}", path.display())))?
        .to_string();
    Ok((name, ext_suffix))
}

fn single_wheel(dir: &Path) -> Result<PathBuf> {
    let wheels: Vec<PathBuf> = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e == "whl"))
        .collect();
    match wheels.len() {
        1 => Ok(wheels.into_iter().next().unwrap()),
        n => Err(Error::Wheel(format!(
            "expected exactly one wheel in {}, found {n}",
            dir.display()
        ))),
    }
}

/// Importable top-level names: packages (dirs) and single-file modules,
/// excluding dist-info/data directories.
fn top_level_modules(entries: &[whl::WheelEntry]) -> Vec<String> {
    let mut names = std::collections::BTreeSet::new();
    for entry in entries {
        let top = entry.name.split('/').next().unwrap_or("");
        if top.is_empty()
            || top.ends_with(".dist-info")
            || top.ends_with(".data")
            || top.ends_with(".libs")
        {
            continue;
        }
        if entry.name.contains('/') {
            names.insert(top.to_string());
        } else if let Some(stem) = top.split('.').next() {
            if top.ends_with(".py") || top.contains(".so") {
                names.insert(stem.to_string());
            }
        }
    }
    names.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_requires_defaults_without_pyproject() {
        let dir = tempfile::tempdir().unwrap();
        let requires = build_requires(dir.path()).unwrap();
        assert!(requires.iter().any(|r| r.starts_with("setuptools")));
    }

    #[test]
    fn build_requires_reads_pyproject() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[build-system]\nrequires = [\"scikit-build-core>=0.10\", \"nanobind>=2\"]\n",
        )
        .unwrap();
        let requires = build_requires(dir.path()).unwrap();
        assert_eq!(requires, vec!["scikit-build-core>=0.10", "nanobind>=2"]);
    }

    #[test]
    fn top_level_module_detection() {
        let entries = vec![
            whl::WheelEntry {
                name: "pkg/__init__.py".into(),
                data: vec![],
                mode: 0o644,
            },
            whl::WheelEntry {
                name: "flat.cpython-312-x86_64-linux-gnu.so".into(),
                data: vec![],
                mode: 0o755,
            },
            whl::WheelEntry {
                name: "demo-1.0.dist-info/RECORD".into(),
                data: vec![],
                mode: 0o644,
            },
        ];
        assert_eq!(top_level_modules(&entries), vec!["flat", "pkg"]);
    }

    #[test]
    fn cargo_target_env_naming() {
        assert_eq!(
            cargo_target_env("aarch64-unknown-linux-gnu"),
            "AARCH64_UNKNOWN_LINUX_GNU"
        );
        assert_eq!(
            cargo_target_env("x86_64-unknown-linux-gnu"),
            "X86_64_UNKNOWN_LINUX_GNU"
        );
    }

    #[test]
    fn builtin_sources_include_pip() {
        let sources = ToolchainSources::builtin();
        let pip = sources.get("pip", PIP_VERSION).unwrap();
        assert!(pip.url().ends_with(".whl"));
        assert_eq!(pip.sha256.len(), 64);
    }
}
