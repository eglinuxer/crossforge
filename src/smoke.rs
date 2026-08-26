//! Built-in toolchain smoke test: compiles a dlopen'd plugin and a host
//! program with the toolchain under test, audits both against the baseline,
//! and executes them on real baseline runtimes.
//!
//! The test exercises the deepest corner of the nonshared hybrid-linking
//! model: the plugin and the executable each carry their own static copy of
//! nonshared-provided code, and an exception of a nonshared-provided type
//! (`std::filesystem::filesystem_error`) must still be caught by exact type
//! across the DSO boundary.

use std::path::{Path, PathBuf};

use crate::audit::Auditor;
use crate::engine::{Cmd, Runner};
use crate::error::{Error, Result};
use crate::spec::ToolchainSpec;
use crate::target::TargetArch;
use crate::verify::{VerifyResult, verify_in_containers};

const PLUGIN_CPP: &str = include_str!("registry/smoke/plugin.cpp");
const MAIN_CPP: &str = include_str!("registry/smoke/main.cpp");

/// The outcome of one smoke run.
#[derive(Debug, Clone)]
pub struct SmokeOutcome {
    /// Where the compiled artifacts live.
    pub dir: PathBuf,
    /// Per-runtime execution results (container images for x86_64, a single
    /// "qemu" entry for aarch64).
    pub runs: Vec<VerifyResult>,
}

impl SmokeOutcome {
    pub fn passed(&self) -> bool {
        !self.runs.is_empty() && self.runs.iter().all(|r| r.passed)
    }
}

/// Compiles, audits and executes the built-in smoke test.
#[derive(Debug)]
pub struct SmokeRunner<'a, R: Runner> {
    pub runner: &'a R,
    pub work_dir: PathBuf,
}

impl<'a, R: Runner> SmokeRunner<'a, R> {
    /// Runs the smoke test for a built toolchain prefix. `images` is the
    /// container matrix for x86_64 targets (ignored for aarch64, which runs
    /// under user-mode qemu against the toolchain sysroot).
    pub fn run(
        &self,
        spec: &ToolchainSpec,
        prefix: &Path,
        images: &[String],
    ) -> Result<SmokeOutcome> {
        let triple = spec.target.triple();
        let dir = self.work_dir.join(format!("smoke-{}", spec.id()));
        std::fs::create_dir_all(&dir)?;
        std::fs::write(dir.join("plugin.cpp"), PLUGIN_CPP)?;
        std::fs::write(dir.join("main.cpp"), MAIN_CPP)?;
        let gxx = prefix.join("bin").join(format!("{triple}-g++"));
        let logs = dir.join("logs");

        tracing::info!(dir = %dir.display(), "compiling smoke test");
        self.runner.exec(
            &Cmd::new(gxx.display().to_string())
                .args([
                    "-std=c++20",
                    "-fPIC",
                    "-shared",
                    "plugin.cpp",
                    "-o",
                    "libplugin.so",
                ])
                .cwd(&dir)
                .log(logs.join("compile-plugin.log")),
        )?;
        self.runner.exec(
            &Cmd::new(gxx.display().to_string())
                .args(["-std=c++20", "main.cpp", "-ldl", "-o", "smoke"])
                .cwd(&dir)
                .log(logs.join("compile-main.log")),
        )?;

        // Audit both artifacts against the baseline sysroot inside the prefix.
        let sysroot = prefix.join(&triple).join("sysroot");
        let auditor = Auditor::from_sysroot(&sysroot, spec.target)?;
        for artifact in ["smoke", "libplugin.so"] {
            let report = auditor.audit_file(&dir.join(artifact))?;
            if !report.passed() {
                let details: Vec<String> =
                    report.findings.iter().map(|f| f.message.clone()).collect();
                return Err(Error::SmokeAudit {
                    artifact: artifact.to_string(),
                    details: details.join("; "),
                });
            }
        }
        tracing::info!("smoke artifacts audit-clean");

        let runs = match spec.target {
            // The plugin path must be absolute: the container has no cwd set,
            // and the bind mount keeps host paths valid inside it.
            TargetArch::X86_64 => verify_in_containers(
                "docker",
                images,
                &dir.join("smoke"),
                &[dir.join("libplugin.so").display().to_string()],
                &[dir.clone()],
                &logs,
            )?,
            TargetArch::Aarch64 => {
                let log = logs.join("qemu-run.log");
                let passed = self
                    .runner
                    .exec(
                        &Cmd::new("qemu-aarch64")
                            .args([
                                "-L".to_string(),
                                sysroot.display().to_string(),
                                "./smoke".to_string(),
                                "./libplugin.so".to_string(),
                            ])
                            .cwd(&dir)
                            .log(&log),
                    )
                    .is_ok();
                vec![VerifyResult {
                    image: "qemu-aarch64".to_string(),
                    passed,
                    log,
                }]
            }
        };
        for r in &runs {
            tracing::info!(runtime = %r.image, passed = r.passed, "smoke run");
        }
        Ok(SmokeOutcome { dir, runs })
    }
}
