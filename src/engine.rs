use std::path::{Path, PathBuf};
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

impl<R: Runner + ?Sized> Runner for &R {
    fn exec(&self, cmd: &Cmd) -> Result<()> {
        (**self).exec(cmd)
    }
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
            .join(spec.sysroot_id());
        let profile = sources.profile(&spec.sysroot_profile)?;
        let sysroot = SysrootGenerator::new(&fetcher, &sources, self.config.mirror.clone())
            .generate(baseline, spec.target, &profile, &sysroot_dir)?;

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
        let triple = spec.target.triple();

        // The compiler and its target runtime libraries depend on (gcc,
        // binutils, baseline, target) and nothing else — a deeper sysroot
        // adds headers and libraries for the *user's* code, not for
        // libstdc++ or libgcc. Verified between the minimal and qt6
        // prefixes: identical c++config.h, identical libstdc++ exported
        // symbol set (6274), identical predefined macros, and a configure
        // line differing only in the prefix and sysroot paths, which GCC
        // resolves relative to itself.
        //
        // So a non-default profile clones the base toolchain and swaps the
        // sysroot instead of spending another full GCC build on producing
        // the same compiler.
        if spec.sysroot_profile != crate::source::DEFAULT_PROFILE && !prefix.join("bin").is_dir() {
            let base_spec = ToolchainSpec {
                sysroot_profile: crate::source::DEFAULT_PROFILE.to_string(),
                ..spec.clone()
            };
            tracing::info!(
                base = %base_spec.id(),
                profile = %spec.sysroot_profile,
                "reusing the base toolchain for this sysroot profile"
            );
            let base = self.build(&base_spec, registry)?;
            self.clone_with_sysroot(&base.root, &prefix, &sysroot.root, &triple)?;
            write_cmake_toolchain_file(&prefix, spec)?;
            write_env_script(&prefix, spec)?;
            write_hardened_specs(&prefix)?;
            return Ok(ToolchainArtifact {
                root: prefix,
                spec: spec.clone(),
            });
        }

        let compiler = builder.build(spec, baseline, &sysroot, &prefix)?;

        let compat = CompatBuilder {
            runner: &self.runner,
            work_dir: self.config.work_dir.join("build"),
        };
        compat.build(spec, baseline, &compiler)?;

        write_cmake_toolchain_file(&compiler.prefix, spec)?;
        write_env_script(&compiler.prefix, spec)?;
        write_hardened_specs(&compiler.prefix)?;

        Ok(ToolchainArtifact {
            root: compiler.prefix,
            spec: spec.clone(),
        })
    }

    /// Copies a built toolchain to `prefix` and replaces its sysroot with
    /// `sysroot`. The compiler resolves its sysroot relative to its own
    /// location, so the clone targets the new baseline content without being
    /// rebuilt.
    ///
    /// Note the GCC build tree belongs to the base toolchain, so
    /// `crossforge check` runs against that one — a cloned profile has no
    /// tree of its own, which is correct: they share a compiler.
    fn clone_with_sysroot(
        &self,
        base: &Path,
        prefix: &Path,
        sysroot: &Path,
        triple: &str,
    ) -> Result<()> {
        let logs = self.config.work_dir.join("build/logs");
        std::fs::create_dir_all(prefix)?;
        self.runner.exec(
            &Cmd::new("cp")
                .args([
                    "-a",
                    &format!("{}/.", base.display()),
                    &prefix.display().to_string(),
                ])
                .log(logs.join("clone-toolchain.log")),
        )?;
        let dest = prefix.join(triple).join("sysroot");
        if dest.exists() {
            std::fs::remove_dir_all(&dest)?;
        }
        std::fs::create_dir_all(&dest)?;
        self.runner.exec(
            &Cmd::new("cp")
                .args([
                    "-a",
                    &format!("{}/.", sysroot.display()),
                    &dest.display().to_string(),
                ])
                .log(logs.join("clone-toolchain.log")),
        )?;
        tracing::info!(prefix = %prefix.display(), "toolchain cloned with the profile sysroot");
        Ok(())
    }
}

/// Writes `toolchain.cmake` into the prefix so CMake projects cross-compile
/// correctly out of the box. Without CMAKE_SYSTEM_NAME/PROCESSOR CMake treats
/// a cross compiler as native and host-arch-sensitive logic misfires (e.g.
/// vcpkg-tool selects x86-era libcurl headers for an aarch64 build). Paths
/// are derived from the file's own location, keeping the prefix relocatable.
fn write_cmake_toolchain_file(prefix: &Path, spec: &ToolchainSpec) -> Result<()> {
    let triple = spec.target.triple();
    let content = format!(
        "# crossforge toolchain file for {triple} (baseline {baseline}).\n\
         # Usage: cmake -DCMAKE_TOOLCHAIN_FILE=<prefix>/toolchain.cmake ...\n\
         set(CMAKE_SYSTEM_NAME Linux)\n\
         set(CMAKE_SYSTEM_PROCESSOR {arch})\n\
         get_filename_component(_crossforge_root \"${{CMAKE_CURRENT_LIST_DIR}}\" ABSOLUTE)\n\
         set(CMAKE_C_COMPILER \"${{_crossforge_root}}/bin/{triple}-gcc\")\n\
         set(CMAKE_CXX_COMPILER \"${{_crossforge_root}}/bin/{triple}-g++\")\n\
         set(CMAKE_AR \"${{_crossforge_root}}/bin/{triple}-ar\")\n\
         set(CMAKE_RANLIB \"${{_crossforge_root}}/bin/{triple}-ranlib\")\n\
         set(CMAKE_STRIP \"${{_crossforge_root}}/bin/{triple}-strip\")\n\
         set(CMAKE_SYSROOT \"${{_crossforge_root}}/{triple}/sysroot\")\n\
         set(CMAKE_FIND_ROOT_PATH \"${{_crossforge_root}}/{triple}/sysroot\")\n\
         set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)\n\
         set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)\n\
         set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)\n\
         set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)\n\
         # pkg-config must read the target's .pc files, with paths rewritten\n\
         # into the sysroot (a plain PKG_CONFIG_PATH would leak host libs).\n\
         set(ENV{{PKG_CONFIG_SYSROOT_DIR}} \"${{_crossforge_root}}/{triple}/sysroot\")\n\
         set(ENV{{PKG_CONFIG_LIBDIR}} \"${{_crossforge_root}}/{triple}/sysroot/usr/lib64/pkgconfig:${{_crossforge_root}}/{triple}/sysroot/usr/share/pkgconfig\")\n",
        triple = triple,
        baseline = spec.baseline,
        arch = spec.target.as_str(),
    );
    let path = prefix.join("toolchain.cmake");
    std::fs::write(&path, content)?;
    tracing::info!(file = %path.display(), "CMake toolchain file written");
    Ok(())
}

/// Writes `crossenv.sh`: the same environment the CMake toolchain file sets,
/// for build systems that are not CMake (autotools, meson, plain make). It
/// locates itself, so the prefix stays relocatable, and regenerates the
/// meson cross file on each source since meson cannot express relative paths.
fn write_env_script(prefix: &Path, spec: &ToolchainSpec) -> Result<()> {
    let triple = spec.target.triple();
    let arch = spec.target.as_str();
    let content = format!(
        r#"# crossforge environment for {triple} (baseline {baseline}, sysroot profile {profile}).
# Usage: . <prefix>/crossenv.sh
CROSSFORGE_ROOT="$(cd "$(dirname "${{BASH_SOURCE[0]:-$0}}")" && pwd)"
CROSSFORGE_SYSROOT="$CROSSFORGE_ROOT/{triple}/sysroot"
CROSSFORGE_TRIPLE="{triple}"
export CROSSFORGE_ROOT CROSSFORGE_SYSROOT CROSSFORGE_TRIPLE
export PATH="$CROSSFORGE_ROOT/bin:$PATH"
export CC="{triple}-gcc"
export CXX="{triple}-g++"
export AR="{triple}-ar"
export RANLIB="{triple}-ranlib"
export STRIP="{triple}-strip"
export LD="{triple}-ld"
export NM="{triple}-nm"
export OBJCOPY="{triple}-objcopy"
export OBJDUMP="{triple}-objdump"
export READELF="{triple}-readelf"
export PKG_CONFIG_SYSROOT_DIR="$CROSSFORGE_SYSROOT"
export PKG_CONFIG_LIBDIR="$CROSSFORGE_SYSROOT/usr/lib64/pkgconfig:$CROSSFORGE_SYSROOT/usr/share/pkgconfig"
export CMAKE_TOOLCHAIN_FILE="$CROSSFORGE_ROOT/toolchain.cmake"

# meson cannot express relative paths in a cross file, so regenerate it here.
cat > "$CROSSFORGE_ROOT/meson-cross.ini" <<EOF
[binaries]
c = '$CROSSFORGE_ROOT/bin/{triple}-gcc'
cpp = '$CROSSFORGE_ROOT/bin/{triple}-g++'
ar = '$CROSSFORGE_ROOT/bin/{triple}-ar'
strip = '$CROSSFORGE_ROOT/bin/{triple}-strip'
pkg-config = 'pkg-config'

[host_machine]
system = 'linux'
cpu_family = '{arch}'
cpu = '{arch}'
endian = 'little'

[properties]
sys_root = '$CROSSFORGE_SYSROOT'
pkg_config_libdir = '$CROSSFORGE_SYSROOT/usr/lib64/pkgconfig:$CROSSFORGE_SYSROOT/usr/share/pkgconfig'
EOF
export CROSSFORGE_MESON_CROSS="$CROSSFORGE_ROOT/meson-cross.ini"
"#,
        triple = triple,
        arch = arch,
        baseline = spec.baseline,
        profile = spec.sysroot_profile,
    );
    let path = prefix.join("crossenv.sh");
    std::fs::write(&path, content)?;
    tracing::info!(file = %path.display(), "environment script written");
    Ok(())
}

/// Writes an opt-in hardened spec file into the prefix.
///
/// The toolchain deliberately does not harden by default: silently changing
/// what a compiler emits is how build systems acquire mysteries, and a
/// toolchain's job is to be predictable. Red Hat's system compilers do bake
/// these in, which is a distribution policy, not a compiler one — so the
/// same options ship here as something a build opts into:
///
///     gcc -specs=<prefix>/share/crossforge/hardened.specs -O2 ...
///
/// Each conditional in the file was verified against this compiler: the
/// FORTIFY define appears for C and C++ when optimizing, is skipped at -O0
/// (where glibc would warn), and yields to a level the caller set itself.
///
/// PIE is left out on purpose. It is the one hardening option that changes
/// linking enough to break projects with non-PIC static libraries, and the
/// failure is a link error in someone else's build — add `-fPIE -pie` to opt
/// into that separately.
fn write_hardened_specs(prefix: &Path) -> Result<()> {
    let dir = prefix.join("share/crossforge");
    std::fs::create_dir_all(&dir)?;
    let content = "\
# crossforge hardened specs — opt in with -specs=<this file>.
#
#   -fstack-protector-strong    stack canaries on functions with local arrays
#   -D_FORTIFY_SOURCE=2         checked variants of the string/memory builtins
#                               (only while optimizing, and never overriding a
#                               level the caller chose)
#   -z relro -z now             full RELRO: the GOT is read-only after startup
#
# Not included: -fPIE -pie, which changes linking enough to break projects
# with non-PIC static libraries. Add it explicitly if you want it.

*cc1_options:
+ -fstack-protector-strong %{O*:%{!O0:%{!D_FORTIFY_SOURCE*:-D_FORTIFY_SOURCE=2}}}

*link:
+ -z relro -z now
";
    let path = dir.join("hardened.specs");
    std::fs::write(&path, content)?;
    tracing::info!(file = %path.display(), "hardened spec file written");
    Ok(())
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
