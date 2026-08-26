use std::path::PathBuf;
use std::process::Stdio;

use crate::baseline::BaselineRegistry;
use crate::compat::CompatBuilder;
use crate::compiler::{CompilerBuilder, ToolchainSources};
use crate::error::{Error, Result};
use crate::fetch::Fetcher;
use crate::source::SourceRegistry;
use crate::spec::ToolchainSpec;
use crate::sysroot::SysrootGenerator;

/// A command to run, described abstractly so runners can translate it —
/// [`LocalRunner`] executes it directly, [`ContainerRunner`] wraps it in
/// `docker`/`podman run`.
#[derive(Debug, Clone, Default)]
pub struct Cmd {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: Option<PathBuf>,
    pub env: Vec<(String, String)>,
    /// Redirect stdout+stderr (append) to this file.
    pub log_file: Option<PathBuf>,
}

impl Cmd {
    pub fn new(program: impl Into<String>) -> Self {
        Self {
            program: program.into(),
            ..Default::default()
        }
    }

    pub fn arg(mut self, arg: impl Into<String>) -> Self {
        self.args.push(arg.into());
        self
    }

    pub fn args<I: IntoIterator<Item = S>, S: Into<String>>(mut self, args: I) -> Self {
        self.args.extend(args.into_iter().map(Into::into));
        self
    }

    pub fn cwd(mut self, dir: impl Into<PathBuf>) -> Self {
        self.cwd = Some(dir.into());
        self
    }

    pub fn env(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.env.push((key.into(), value.into()));
        self
    }

    pub fn log(mut self, file: impl Into<PathBuf>) -> Self {
        self.log_file = Some(file.into());
        self
    }
}

/// Command-execution abstraction: the host environment needed to build GCC
/// (an el8 container etc.) is injected by implementing this trait.
pub trait Runner {
    /// Runs the command to completion; a non-zero exit must map to
    /// [`Error::CommandFailed`].
    fn exec(&self, cmd: &Cmd) -> Result<()>;
}

fn spawn(mut command: std::process::Command, cmd: &Cmd) -> Result<()> {
    if let Some(log) = &cmd.log_file {
        if let Some(parent) = log.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let out = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(log)?;
        let err = out.try_clone()?;
        command.stdout(Stdio::from(out)).stderr(Stdio::from(err));
    }
    let status = command.status()?;
    if !status.success() {
        let mut program = cmd.program.clone();
        if let Some(log) = &cmd.log_file {
            program.push_str(&format!(" (log: {})", log.display()));
        }
        return Err(Error::CommandFailed { program, status });
    }
    Ok(())
}

/// Runs commands directly on the current host.
#[derive(Debug, Default, Clone, Copy)]
pub struct LocalRunner;

impl Runner for LocalRunner {
    fn exec(&self, cmd: &Cmd) -> Result<()> {
        tracing::debug!(program = %cmd.program, args = ?cmd.args, "exec (local)");
        let mut command = std::process::Command::new(&cmd.program);
        command.args(&cmd.args);
        if let Some(dir) = &cmd.cwd {
            command.current_dir(dir);
        }
        for (k, v) in &cmd.env {
            command.env(k, v);
        }
        spawn(command, cmd)
    }
}

/// Runs each command inside a fresh container (`docker run --rm` / `podman
/// run --rm`), bind-mounting `binds` at identical paths so host-side paths
/// stay valid inside the container.
///
/// The image must provide the build tools (gcc, g++, make, tar, xz, bzip2).
#[derive(Debug, Clone)]
pub struct ContainerRunner {
    /// Container engine binary: `docker` or `podman`.
    pub engine: String,
    /// Image name, e.g. `crossforge-buildenv:el8`.
    pub image: String,
    /// Host directories mounted at the same path inside the container.
    pub binds: Vec<PathBuf>,
    /// `uid:gid` to run as; `None` uses the image default.
    pub user: Option<String>,
}

impl Runner for ContainerRunner {
    fn exec(&self, cmd: &Cmd) -> Result<()> {
        tracing::debug!(program = %cmd.program, args = ?cmd.args, image = %self.image, "exec (container)");
        let mut command = std::process::Command::new(&self.engine);
        command.args(["run", "--rm"]);
        if let Some(user) = &self.user {
            command.args(["--user", user]);
        }
        for bind in &self.binds {
            let path = bind.display();
            command.args(["-v", &format!("{path}:{path}")]);
        }
        if let Some(dir) = &cmd.cwd {
            command.args(["-w", &dir.display().to_string()]);
        }
        for (k, v) in &cmd.env {
            command.args(["-e", &format!("{k}={v}")]);
        }
        command.arg(&self.image);
        command.arg(&cmd.program);
        command.args(&cmd.args);
        spawn(command, cmd)
    }
}

/// Build-engine configuration.
#[derive(Debug, Clone)]
pub struct BuildConfig {
    /// Working directory for intermediate stage outputs and final artifacts.
    pub work_dir: PathBuf,
    /// Download cache (source tarballs, distro RPMs; content-addressed).
    pub cache_dir: PathBuf,
    /// Mirror prefix for distro packages; `None` uses upstream defaults.
    pub mirror: Option<String>,
    /// Parallel build jobs; `None` uses the host's available parallelism.
    pub jobs: Option<usize>,
}

/// A finished, relocatable toolchain.
#[derive(Debug, Clone)]
pub struct ToolchainArtifact {
    /// Toolchain root directory (freely movable).
    pub root: PathBuf,
    /// The spec it was built from.
    pub spec: ToolchainSpec,
}

/// Toolchain build engine: drives the sysroot → compiler → compat → pack
/// pipeline for a [`ToolchainSpec`].
#[derive(Debug)]
pub struct BuildEngine<R = LocalRunner> {
    config: BuildConfig,
    runner: R,
}

impl BuildEngine<LocalRunner> {
    pub fn new(config: BuildConfig) -> Self {
        Self::with_runner(config, LocalRunner)
    }
}

impl<R: Runner> BuildEngine<R> {
    pub fn with_runner(config: BuildConfig, runner: R) -> Self {
        Self { config, runner }
    }

    pub fn config(&self) -> &BuildConfig {
        &self.config
    }

    pub fn runner(&self) -> &R {
        &self.runner
    }

    /// Builds one complete toolchain.
    ///
    /// Pipeline (design doc §5): sysroot (M1) → compiler (M2) → compat (M3,
    /// not yet wired in) → pack. Idempotent per stage: existing artifacts are
    /// reused.
    pub fn build(
        &self,
        spec: &ToolchainSpec,
        registry: &BaselineRegistry,
    ) -> Result<ToolchainArtifact> {
        let baseline = registry
            .get(&spec.baseline)
            .ok_or_else(|| Error::UnknownBaseline(spec.baseline.clone()))?;
        tracing::info!(id = %spec.id(), triple = %spec.target.triple(), "toolchain build requested");

        let fetcher = Fetcher::new(self.config.cache_dir.clone())?;
        let sources = SourceRegistry::builtin();
        let sysroot_dir = self
            .config
            .work_dir
            .join("sysroots")
            .join(format!("{}-{}", baseline.alias, spec.target));
        let sysroot = SysrootGenerator::new(&fetcher, &sources, self.config.mirror.clone())
            .generate(baseline, spec.target, &sysroot_dir)?;

        let jobs = self.config.jobs.unwrap_or_else(|| {
            std::thread::available_parallelism()
                .map(|n| n.get())
                .unwrap_or(4)
        });
        let builder = CompilerBuilder {
            fetcher: &fetcher,
            runner: &self.runner,
            sources: ToolchainSources::builtin(),
            work_dir: self.config.work_dir.join("build"),
            jobs,
        };
        let prefix = self.config.work_dir.join("toolchains").join(spec.id());
        let compiler = builder.build(spec, baseline, &sysroot, &prefix)?;

        let compat = CompatBuilder {
            runner: &self.runner,
            work_dir: self.config.work_dir.join("build"),
        };
        compat.build(spec, baseline, &compiler)?;

        Ok(ToolchainArtifact {
            root: compiler.prefix,
            spec: spec.clone(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_runner_reports_failure() {
        let runner = LocalRunner;
        let err = runner.exec(&Cmd::new("false"));
        assert!(matches!(err, Err(Error::CommandFailed { .. })));
        runner.exec(&Cmd::new("true")).unwrap();
    }

    #[test]
    fn local_runner_logs_output() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("logs/echo.log");
        let runner = LocalRunner;
        runner
            .exec(&Cmd::new("sh").args(["-c", "echo hello-log"]).log(&log))
            .unwrap();
        let content = std::fs::read_to_string(&log).unwrap();
        assert!(content.contains("hello-log"));
    }

    #[test]
    fn cmd_builder_collects_fields() {
        let cmd = Cmd::new("make")
            .arg("-j8")
            .cwd("/build")
            .env("PATH", "/x/bin");
        assert_eq!(cmd.program, "make");
        assert_eq!(cmd.args, vec!["-j8"]);
        assert_eq!(cmd.cwd.as_deref(), Some(std::path::Path::new("/build")));
        assert_eq!(cmd.env, vec![("PATH".to_string(), "/x/bin".to_string())]);
    }
}
