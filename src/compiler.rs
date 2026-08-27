//! Compiler build orchestration (design doc §5.2, milestone M2).
//!
//! Builds cross binutils + GCC against a pre-assembled binary sysroot. Because
//! the sysroot already contains a complete glibc, no multi-stage bootstrap is
//! needed: binutils, then GCC (compiler + target runtime libs) in one pass.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::baseline::BaselineDef;
use crate::engine::{Cmd, Runner};
use crate::error::{Error, Result};
use crate::fetch::Fetcher;
use crate::spec::ToolchainSpec;
use crate::sysroot::SysrootArtifact;

/// GCC in-tree prerequisite libraries, pinned to the versions listed by
/// GCC's `contrib/download_prerequisites` (11.x list; satisfies GCC 14 minimums).
/// Where the build tree is recorded as living, in anything the toolchain
/// ships. Distributions use `/usr/src/debug/...` for the same purpose.
const CANONICAL_BUILD_ROOT: &str = "/usr/src/crossforge";

/// gdb version built alongside each toolchain.
pub const DEFAULT_GDB: &str = "17.2";

const GCC_PREREQS: &[(&str, &str)] = &[("gmp", "6.1.0"), ("mpfr", "3.1.4"), ("mpc", "1.0.3")];

/// How a source component is packaged upstream.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ComponentKind {
    /// A plain source tarball.
    #[default]
    Tarball,
    /// A source RPM carrying a tarball + patch series + spec (the RH
    /// gcc-toolset shape); patches are applied in spec `%prep` order.
    Srpm,
}

/// One upstream source component needed to build a toolchain.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ComponentDef {
    pub name: String,
    pub version: String,
    /// URL template with a `{version}` placeholder.
    pub url: String,
    /// Alternate URL templates, tried in order when `url` is unreachable.
    /// Pick hosts that fail independently — a second path on the same server
    /// buys nothing.
    #[serde(default)]
    pub mirrors: Vec<String>,
    pub sha256: String,
    #[serde(default)]
    pub kind: ComponentKind,
    /// Patch file names to skip (conditional patches whose predicates we do
    /// not build with, e.g. isl/docs patches).
    #[serde(default)]
    pub skip_patches: Vec<String>,
}

impl ComponentDef {
    pub fn url(&self) -> String {
        self.url.replace("{version}", &self.version)
    }

    /// Every source for this component, primary first.
    pub fn urls(&self) -> Vec<String> {
        std::iter::once(&self.url)
            .chain(&self.mirrors)
            .map(|u| u.replace("{version}", &self.version))
            .collect()
    }

    /// Source directory name after unpacking, e.g. `binutils-2.40`. For SRPM
    /// components the real directory is discovered after extraction.
    pub fn dir_name(&self) -> String {
        format!("{}-{}", self.name, self.version)
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SourcesFile {
    component: Vec<ComponentDef>,
}

/// Registry of upstream source tarballs, keyed by (name, version).
#[derive(Debug, Clone, Default)]
pub struct ToolchainSources {
    entries: BTreeMap<(String, String), ComponentDef>,
}

const BUILTIN_TOML: &str = include_str!("registry/toolchain-sources.toml");

impl ToolchainSources {
    pub fn builtin() -> Self {
        Self::from_toml(BUILTIN_TOML).expect("builtin toolchain sources must parse")
    }

    pub fn from_toml(text: &str) -> Result<Self> {
        let mut sources = Self::default();
        sources.merge_toml(text)?;
        Ok(sources)
    }

    /// Merges a TOML document; entries override existing (name, version) keys.
    pub fn merge_toml(&mut self, text: &str) -> Result<()> {
        let file: SourcesFile = toml::from_str(text)?;
        for def in file.component {
            self.entries
                .insert((def.name.clone(), def.version.clone()), def);
        }
        Ok(())
    }

    pub fn get(&self, name: &str, version: &str) -> Result<&ComponentDef> {
        self.entries
            .get(&(name.to_string(), version.to_string()))
            .ok_or_else(|| Error::UnknownComponent {
                name: name.to_string(),
                version: version.to_string(),
            })
    }
}

/// A built cross compiler installation.
#[derive(Debug, Clone)]
pub struct CompilerArtifact {
    /// Installation prefix (relocatable).
    pub prefix: PathBuf,
    /// Target triple, e.g. `x86_64-unknown-linux-gnu`.
    pub triple: String,
}

/// Builds cross binutils + GCC into an installation prefix.
#[derive(Debug)]
pub struct CompilerBuilder<'a, R: Runner> {
    pub fetcher: &'a Fetcher,
    pub runner: &'a R,
    pub sources: ToolchainSources,
    /// Directory for source trees, build trees and logs.
    pub work_dir: PathBuf,
    pub jobs: usize,
}

impl<'a, R: Runner> CompilerBuilder<'a, R> {
    /// Builds the compiler described by `spec` against `sysroot`, installing
    /// into `prefix`. Idempotent: an existing `<prefix>/bin/<triple>-gcc`
    /// short-circuits the build.
    pub fn build(
        &self,
        spec: &ToolchainSpec,
        baseline: &BaselineDef,
        sysroot: &SysrootArtifact,
        prefix: &Path,
    ) -> Result<CompilerArtifact> {
        let triple = spec.target.triple();
        let gcc_bin = prefix.join("bin").join(format!("{triple}-gcc"));
        if gcc_bin.is_file() {
            tracing::info!(prefix = %prefix.display(), "compiler already built, skipping");
            // Cheap and idempotent, so it also repairs prefixes built before
            // aliases existed.
            if spec.target == crate::target::TargetArch::X86_64 {
                install_native_aliases(&prefix.join("bin"), &triple)?;
            }
            return Ok(CompilerArtifact {
                prefix: prefix.to_path_buf(),
                triple,
            });
        }

        let src_dir = self.work_dir.join("src");
        let logs = self.work_dir.join("logs").join(spec.id());
        std::fs::create_dir_all(&src_dir)?;
        std::fs::create_dir_all(&logs)?;

        // 1. Fetch and unpack sources.
        let binutils = self.sources.get("binutils", &spec.binutils)?.clone();
        let gcc = self.sources.get("gcc", &spec.gcc)?.clone();
        let binutils_src = self.unpack(&binutils, &src_dir, &logs)?;
        let gcc_src = self.unpack(&gcc, &src_dir, &logs)?;
        for (name, version) in GCC_PREREQS {
            let prereq = self.sources.get(name, version)?.clone();
            let prereq_src = self.unpack(&prereq, &src_dir, &logs)?;
            // In-tree prerequisite: gcc's toplevel picks up `gmp/`, `mpfr/`,
            // `mpc/` dirs (symlinks to the unpacked trees, as
            // download_prerequisites does).
            let link = gcc_src.join(name);
            if !link.is_symlink() {
                std::os::unix::fs::symlink(
                    format!("../{}", prereq_src.file_name().unwrap().to_string_lossy()),
                    &link,
                )?;
            }
        }

        // 2. Install the sysroot inside the prefix so the toolchain stays
        // relocatable (GCC translates its configured sysroot when moved, as
        // long as the sysroot lives under the prefix).
        let sysroot_dest = prefix.join(&triple).join("sysroot");
        if !sysroot_dest.join(".crossforge").is_dir() {
            std::fs::create_dir_all(&sysroot_dest)?;
            self.runner.exec(
                &Cmd::new("cp")
                    .args([
                        "-a",
                        &format!("{}/.", sysroot.root.display()),
                        &sysroot_dest.display().to_string(),
                    ])
                    .log(logs.join("copy-sysroot.log")),
            )?;
        }

        // 3. binutils. Build dirs are never reused across runs: a stale
        // config.cache from a different build image breaks sub-configures.
        let build_binutils = self.work_dir.join(format!("build-binutils-{}", spec.id()));
        if build_binutils.exists() {
            std::fs::remove_dir_all(&build_binutils)?;
        }
        std::fs::create_dir_all(&build_binutils)?;
        tracing::info!(version = %binutils.version, "building binutils");
        self.runner.exec(
            &Cmd::new(
                build_binutils
                    .join("..")
                    .join("src")
                    .join(binutils_src.file_name().unwrap())
                    .join("configure")
                    .display()
                    .to_string(),
            )
            .args([
                format!("--target={triple}"),
                format!("--prefix={}", prefix.display()),
                format!("--with-sysroot={}", sysroot_dest.display()),
                "--disable-nls".to_string(),
                "--disable-werror".to_string(),
                "--disable-gdb".to_string(),
                "--disable-gprofng".to_string(),
            ])
            .cwd(&build_binutils)
            .log(logs.join("binutils-configure.log")),
        )?;
        // binutils 2.40's toplevel has a dependency race under high -j: gas
        // can link before libsframe is archived. Build libsframe first
        // (ignore failure on versions without the target).
        let _ = self.make(
            &build_binutils,
            &logs,
            "binutils-make.log",
            &["all-libsframe"],
        );
        self.make(&build_binutils, &logs, "binutils-make.log", &[])?;
        // install-strip keeps host executables free of debug info (a stripped
        // cc1plus is ~35MB instead of ~450MB); target libraries keep theirs.
        self.make(
            &build_binutils,
            &logs,
            "binutils-install.log",
            &["install-strip"],
        )?;

        // 4. GCC (compiler + target runtime libs in one pass; the sysroot
        // already provides a complete glibc). PATH must expose the freshly
        // installed cross binutils.
        let build_gcc = self.work_dir.join(format!("build-gcc-{}", spec.id()));
        if build_gcc.exists() {
            std::fs::remove_dir_all(&build_gcc)?;
        }
        std::fs::create_dir_all(&build_gcc)?;
        let path_env = format!("{}/bin:/usr/local/bin:/usr/bin:/bin", prefix.display());
        tracing::info!(version = %gcc.version, "building gcc (this takes a while)");
        let mut configure_args = vec![
            format!("--target={triple}"),
            format!("--prefix={}", prefix.display()),
            format!("--with-sysroot={}", sysroot_dest.display()),
            "--enable-languages=c,c++".to_string(),
            "--disable-multilib".to_string(),
            "--disable-bootstrap".to_string(),
            "--disable-nls".to_string(),
            "--without-isl".to_string(),
        ];
        if !baseline.cxx11_abi {
            // The baseline libstdc++ predates the CXX11 string/list ABI;
            // default the compiler to the old ABI (design doc §3.2).
            tracing::warn!(
                baseline = %baseline.alias,
                "baseline forces the old std::string ABI (_GLIBCXX_USE_CXX11_ABI=0)"
            );
            configure_args.push("--with-default-libstdcxx-abi=gcc4-compatible".to_string());
        }
        // Target-library flags aligned with RH optflags: _GLIBCXX_ASSERTIONS
        // disables libstdc++'s extern-template declarations, which the RH
        // nonshared objects rely on to emit their hidden weak instantiations
        // (without it, `.hidden` references stay undefined and links fail).
        // The nonshared archive is linked *into user binaries*, so any path
        // baked into it travels there — this build machine's directory
        // layout would end up inside everyone's artifacts, and would differ
        // between two builds of the same source. Mapping the whole build
        // tree to a canonical location fixes both: the recorded paths become
        // stable and meaningful (the shape distributions use for debug
        // sources) rather than an accident of where crossforge happened to
        // run.
        let prefix_map = format!(
            "-ffile-prefix-map={}={CANONICAL_BUILD_ROOT}",
            self.work_dir.display()
        );
        let target_cxxflags = format!("-g -O2 -D_GLIBCXX_ASSERTIONS {prefix_map}");
        let target_cflags = format!("-g -O2 {prefix_map}");
        self.runner.exec(
            &Cmd::new(gcc_src.join("configure").display().to_string())
                .args(configure_args)
                .cwd(&build_gcc)
                .env("PATH", &path_env)
                .env("CXXFLAGS_FOR_TARGET", &target_cxxflags)
                .env("CFLAGS_FOR_TARGET", &target_cflags)
                .log(logs.join("gcc-configure.log")),
        )?;
        self.make_with_env(&build_gcc, &logs, "gcc-make.log", &[], &path_env)?;
        self.make_with_env(
            &build_gcc,
            &logs,
            "gcc-install.log",
            &["install-strip"],
            &path_env,
        )?;

        // 4b. RH compat patches produce libstdc++_nonshared<NN>.a as side
        // products of the libstdc++ build; keep them for the compat stage.
        let nonshared_src = build_gcc.join(&triple).join("libstdc++-v3/src/.libs");
        if nonshared_src.is_dir() {
            for entry in std::fs::read_dir(&nonshared_src)? {
                let path = entry?.path();
                let name = path.file_name().unwrap().to_string_lossy().into_owned();
                if name.starts_with("libstdc++_nonshared") && name.ends_with(".a") {
                    std::fs::copy(&path, prefix.join(&triple).join("lib64").join(&name))?;
                    tracing::info!(archive = %name, "RH nonshared archive installed");
                }
            }
        }

        // 4c. A toolchain whose target is the host architecture doubles as
        // the native compiler for host-side build tools (Qt's moc/rcc/uic,
        // protoc, ... — anything a cross build must run while building).
        // Without unprefixed drivers those builds silently fall back to the
        // distro's system gcc, which on el8 is 8.5 and not the compiler this
        // toolchain was qualified as.
        if spec.target == crate::target::TargetArch::X86_64 {
            install_native_aliases(&prefix.join("bin"), &triple)?;
        }

        // 4d. Cross gdb. A toolchain that can build for another
        // architecture but cannot inspect what it built is half a tool.
        self.build_gdb(spec, &sysroot_dest, prefix, &logs)?;

        // 5. Smoke check.
        self.runner.exec(
            &Cmd::new(gcc_bin.display().to_string())
                .arg("--version")
                .log(logs.join("smoke.log")),
        )?;
        tracing::info!(prefix = %prefix.display(), "compiler built");
        Ok(CompilerArtifact {
            prefix: prefix.to_path_buf(),
            triple,
        })
    }

    /// Builds expat and MPFR as static host libraries, once per work dir.
    ///
    /// gdb needs both. The build container has them as packages, but linking
    /// against those would tie the debugger to `libmpfr.so.4` — an el8-only
    /// soname absent from Ubuntu and Debian — while everything else in the
    /// prefix runs on any host with glibc >= 2.28. Static keeps gdb as
    /// portable as the compiler beside it.
    fn build_host_deps(&self, logs: &Path) -> Result<PathBuf> {
        let out = self.work_dir.join("hostdeps");
        if out.join("lib/libexpat.a").is_file() && out.join("lib/libmpfr.a").is_file() {
            return Ok(out);
        }
        let src_dir = self.work_dir.join("src");
        for (name, version, extra) in [
            (
                "expat",
                "2.8.3",
                vec!["--without-docbook", "--without-tests"],
            ),
            ("mpfr", "3.1.4", vec![]),
        ] {
            let component = self.sources.get(name, version)?.clone();
            let source = self.unpack(&component, &src_dir, logs)?;
            let build = self.work_dir.join(format!("build-{name}-host"));
            if build.exists() {
                std::fs::remove_dir_all(&build)?;
            }
            std::fs::create_dir_all(&build)?;
            tracing::info!(component = %component.dir_name(), "building host dependency");
            let mut args = vec![
                format!("--prefix={}", out.display()),
                "--disable-shared".to_string(),
                "--enable-static".to_string(),
            ];
            args.extend(extra.into_iter().map(String::from));
            self.runner.exec(
                &Cmd::new(source.join("configure").display().to_string())
                    .args(args)
                    .cwd(&build)
                    .log(logs.join("hostdeps.log")),
            )?;
            self.make(&build, logs, "hostdeps.log", &[])?;
            self.make(&build, logs, "hostdeps.log", &["install"])?;
        }
        Ok(out)
    }

    /// Builds `<triple>-gdb` into the prefix.
    fn build_gdb(
        &self,
        spec: &ToolchainSpec,
        sysroot: &Path,
        prefix: &Path,
        logs: &Path,
    ) -> Result<()> {
        let triple = spec.target.triple();
        if prefix.join("bin").join(format!("{triple}-gdb")).is_file() {
            return Ok(());
        }
        let deps = self.build_host_deps(logs)?;
        let component = self.sources.get("gdb", DEFAULT_GDB)?.clone();
        let source = self.unpack(&component, &self.work_dir.join("src"), logs)?;
        let build = self.work_dir.join(format!("build-gdb-{}", spec.id()));
        if build.exists() {
            std::fs::remove_dir_all(&build)?;
        }
        std::fs::create_dir_all(&build)?;
        tracing::info!(version = %component.version, triple = %triple, "building gdb");
        self.runner.exec(
            &Cmd::new(source.join("configure").display().to_string())
                .args([
                    format!("--target={triple}"),
                    format!("--prefix={}", prefix.display()),
                    format!("--with-sysroot={}", sysroot.display()),
                    format!("--with-libexpat-prefix={}", deps.display()),
                    format!("--with-mpfr={}", deps.display()),
                    "--disable-nls".to_string(),
                    "--disable-werror".to_string(),
                    // binutils, ld and gas come from the binutils build; the
                    // gdb tarball carries its own copies of all three.
                    "--disable-binutils".to_string(),
                    "--disable-ld".to_string(),
                    "--disable-gas".to_string(),
                    "--disable-gprofng".to_string(),
                    "--disable-sim".to_string(),
                    "--without-python".to_string(),
                ])
                .cwd(&build)
                .log(logs.join("gdb-configure.log")),
        )?;
        self.make(&build, logs, "gdb-make.log", &["all-gdb"])?;
        self.make(&build, logs, "gdb-install.log", &["install-strip-gdb"])?;
        Ok(())
    }

    /// Fetches a component and unpacks it under `dest`, returning the source
    /// directory. SRPM components additionally get their spec's patch series
    /// applied. Skips work already done.
    fn unpack(&self, component: &ComponentDef, dest: &Path, logs: &Path) -> Result<PathBuf> {
        match component.kind {
            ComponentKind::Tarball => {
                let dir = dest.join(component.dir_name());
                if dir.is_dir() {
                    return Ok(dir);
                }
                let tarball = self
                    .fetcher
                    .fetch_cached_any(&component.urls(), &component.sha256)?;
                tracing::info!(component = %component.dir_name(), "unpacking");
                self.untar(
                    &tarball,
                    dest,
                    &logs.join(format!("unpack-{}.log", component.name)),
                )?;
                if !dir.is_dir() {
                    return Err(Error::Rpm(format!(
                        "tarball for {} did not produce {}",
                        component.name,
                        dir.display()
                    )));
                }
                Ok(dir)
            }
            ComponentKind::Srpm => self.unpack_srpm(component, dest, logs),
        }
    }

    /// Unpacks an SRPM: extract its cpio payload (tarball + patches + spec),
    /// unpack the main tarball, then apply the spec's `%prep` patch series in
    /// order, honoring `skip_patches`.
    fn unpack_srpm(&self, component: &ComponentDef, dest: &Path, logs: &Path) -> Result<PathBuf> {
        let srpm_dir = dest.join(format!("srpm-{}-{}", component.name, component.version));
        let marker = srpm_dir.join(".crossforge-patched");
        if marker.is_file() {
            let src_dir = std::fs::read_to_string(&marker)?;
            return Ok(PathBuf::from(src_dir.trim()));
        }
        let srpm = self
            .fetcher
            .fetch_cached_any(&component.urls(), &component.sha256)?;
        std::fs::create_dir_all(&srpm_dir)?;
        tracing::info!(component = %component.dir_name(), "extracting srpm");
        crate::rpm::extract_rpm(&std::fs::read(&srpm)?, &srpm_dir)?;

        // Main tarball: the gcc-*.tar.* member.
        let mut tarball = None;
        let mut spec_file = None;
        for entry in std::fs::read_dir(&srpm_dir)? {
            let path = entry?.path();
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            if name.contains(".tar.") && name.starts_with(&format!("{}-", component.name)) {
                tarball = Some((path.clone(), name.clone()));
            }
            if name.ends_with(".spec") {
                spec_file = Some(path.clone());
            }
        }
        let (tarball, tar_name) =
            tarball.ok_or_else(|| Error::Rpm(format!("no {}-*.tar.* in srpm", component.name)))?;
        let spec_file = spec_file.ok_or_else(|| Error::Rpm("no .spec in srpm".to_string()))?;

        // Unpack; the top-level directory matches the tarball basename.
        let dir_name = tar_name
            .trim_end_matches(".xz")
            .trim_end_matches(".gz")
            .trim_end_matches(".bz2")
            .trim_end_matches(".zst")
            .trim_end_matches(".tar");
        let src_dir = dest.join(dir_name);
        if !src_dir.is_dir() {
            tracing::info!(tarball = %tar_name, "unpacking");
            self.untar(
                &tarball,
                dest,
                &logs.join(format!("unpack-{}.log", component.name)),
            )?;
        }
        if !src_dir.is_dir() {
            return Err(Error::Rpm(format!(
                "srpm tarball did not produce {}",
                src_dir.display()
            )));
        }

        // Apply the %prep patch series.
        let spec_text = std::fs::read_to_string(&spec_file)?;
        let patches = parse_spec_patches(&spec_text);
        tracing::info!(total = patches.len(), "applying RH patch series");
        for patch in &patches {
            if component.skip_patches.iter().any(|s| s == &patch.file) {
                tracing::debug!(patch = %patch.file, "skipped (conditional)");
                continue;
            }
            let patch_path = srpm_dir.join(&patch.file);
            if !patch_path.is_file() {
                return Err(Error::Rpm(format!(
                    "patch {} missing from srpm",
                    patch.file
                )));
            }
            self.runner.exec(
                &Cmd::new("patch")
                    .args([
                        format!("-p{}", patch.strip),
                        "--fuzz=0".to_string(),
                        "-s".to_string(),
                        "-i".to_string(),
                        patch_path.display().to_string(),
                    ])
                    .cwd(&src_dir)
                    .log(logs.join(format!("patch-{}.log", component.name))),
            )?;
        }
        std::fs::write(&marker, src_dir.display().to_string())?;
        Ok(src_dir)
    }

    fn untar(&self, tarball: &Path, dest: &Path, log: &Path) -> Result<()> {
        self.runner.exec(
            &Cmd::new("tar")
                .args([
                    "-xf",
                    &tarball.display().to_string(),
                    "-C",
                    &dest.display().to_string(),
                ])
                .log(log),
        )
    }

    fn make(&self, cwd: &Path, logs: &Path, log_name: &str, targets: &[&str]) -> Result<()> {
        self.make_with_env(cwd, logs, log_name, targets, "/usr/local/bin:/usr/bin:/bin")
    }

    fn make_with_env(
        &self,
        cwd: &Path,
        logs: &Path,
        log_name: &str,
        targets: &[&str],
        path_env: &str,
    ) -> Result<()> {
        let mut cmd = Cmd::new("make")
            .arg(format!("-j{}", self.jobs))
            .arg("MAKEINFO=true")
            .cwd(cwd)
            .env("PATH", path_env)
            .log(logs.join(log_name));
        for t in targets {
            cmd = cmd.arg(*t);
        }
        self.runner.exec(&cmd)
    }
}

/// Symlinks unprefixed driver names (`gcc`, `g++`, `ar`, ...) next to the
/// `<triple>-` ones, plus the `cc` / `c++` aliases build systems probe for.
fn install_native_aliases(bin_dir: &Path, triple: &str) -> Result<()> {
    let prefix = format!("{triple}-");
    let mut linked = 0;
    for entry in std::fs::read_dir(bin_dir)? {
        let path = entry?.path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        let Some(bare) = name.strip_prefix(&prefix) else {
            continue;
        };
        // Versioned drivers (gcc-14.2.1) would shadow nothing useful.
        if bare.is_empty() || bare.contains(char::is_numeric) {
            continue;
        }
        let link = bin_dir.join(bare);
        if link.exists() || link.is_symlink() {
            continue;
        }
        std::os::unix::fs::symlink(&name, &link)?;
        linked += 1;
    }
    for (alias, target) in [("cc", "gcc"), ("c++", "g++")] {
        let link = bin_dir.join(alias);
        if !link.exists() && !link.is_symlink() && bin_dir.join(target).exists() {
            std::os::unix::fs::symlink(target, &link)?;
            linked += 1;
        }
    }
    tracing::info!(count = linked, "native driver aliases installed");
    Ok(())
}

/// One `%patch` application from a spec's `%prep` section.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SpecPatch {
    pub file: String,
    pub strip: u32,
}

/// Parses `PatchN:` definitions and the `%prep` patch application order from
/// an RPM spec. Both application styles are supported: modern
/// `%patch -PN -pM` (gcc-toolset-14) and legacy `%patchN -pM`
/// (gcc-toolset-11). Conditional (`%if`) guards are not evaluated — unwanted
/// conditional patches are excluded via `skip_patches`.
pub(crate) fn parse_spec_patches(spec: &str) -> Vec<SpecPatch> {
    let mut defs: BTreeMap<u32, String> = BTreeMap::new();
    for line in spec.lines() {
        if let Some(rest) = line.strip_prefix("Patch") {
            if let Some((num, file)) = rest.split_once(':') {
                if let Ok(num) = num.trim().parse::<u32>() {
                    defs.insert(num, file.trim().to_string());
                }
            }
        }
    }
    let mut ordered = Vec::new();
    for line in spec.lines() {
        let line = line.trim();
        let Some(rest) = line.strip_prefix("%patch") else {
            continue;
        };
        // Legacy style carries the number glued to the macro: `%patch7 -p0`.
        let mut number = rest
            .split_whitespace()
            .next()
            .and_then(|t| t.parse::<u32>().ok());
        let mut strip = 1u32;
        for token in rest.split_whitespace() {
            if let Some(n) = token.strip_prefix("-P") {
                number = n.parse::<u32>().ok();
            } else if let Some(p) = token.strip_prefix("-p") {
                strip = p.parse().unwrap_or(1);
            }
        }
        if let Some(file) = number.and_then(|n| defs.get(&n)) {
            ordered.push(SpecPatch {
                file: file.clone(),
                strip,
            });
        }
    }
    ordered
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spec_patch_parsing_modern_style() {
        let spec = "\
Patch0: gcc14-hack.patch
Patch1000: gcc14-libstdc++-compat.patch
Patch5: gcc14-isl-dl.patch

%prep
%patch -P0 -p0 -b .hack~
%if %{build_isl}
%patch -P5 -p0 -b .isl-dl~
%endif
%patch -P1000 -p0 -b .libstdc++-compat~
";
        let patches = parse_spec_patches(spec);
        assert_eq!(
            patches,
            vec![
                SpecPatch {
                    file: "gcc14-hack.patch".to_string(),
                    strip: 0
                },
                SpecPatch {
                    file: "gcc14-isl-dl.patch".to_string(),
                    strip: 0
                },
                SpecPatch {
                    file: "gcc14-libstdc++-compat.patch".to_string(),
                    strip: 0
                },
            ]
        );
    }

    #[test]
    fn spec_patch_parsing_legacy_style() {
        let spec = "\
Patch0: gcc11-hack.patch
Patch100: gcc11-fortran-fdec.patch
Patch2001: doxygen-1.7.1-config.patch

%prep
%patch0 -p0 -b .hack~
%patch100 -p1 -b .fdec~
%patch2001 -p1 -b .config~
";
        let patches = parse_spec_patches(spec);
        assert_eq!(
            patches,
            vec![
                SpecPatch {
                    file: "gcc11-hack.patch".to_string(),
                    strip: 0
                },
                SpecPatch {
                    file: "gcc11-fortran-fdec.patch".to_string(),
                    strip: 1
                },
                SpecPatch {
                    file: "doxygen-1.7.1-config.patch".to_string(),
                    strip: 1
                },
            ]
        );
    }

    #[test]
    fn gdb_has_independent_mirrors() {
        let sources = ToolchainSources::builtin();
        let gdb = sources.get("gdb", DEFAULT_GDB).unwrap();
        let urls = gdb.urls();

        assert_eq!(urls[0], gdb.url(), "the primary must come first");
        assert!(urls.len() >= 3, "one alternate is not enough: {urls:?}");
        assert!(urls.iter().all(|u| u.contains(DEFAULT_GDB)));
        // Mirrors only help when they fail independently, so every source
        // must be a distinct host.
        let mut hosts: Vec<&str> = urls.iter().filter_map(|u| u.split('/').nth(2)).collect();
        hosts.sort_unstable();
        let total = hosts.len();
        hosts.dedup();
        assert_eq!(hosts.len(), total, "duplicate host among {urls:?}");
    }

    #[test]
    fn builtin_sources_cover_default_versions() {
        let sources = ToolchainSources::builtin();
        let gcc = sources.get("gcc", crate::spec::DEFAULT_GCC).unwrap();
        assert_eq!(gcc.kind, ComponentKind::Srpm);
        assert!(gcc.url().contains("gcc-toolset-14-gcc-14.2.1"));
        assert!(gcc.skip_patches.iter().any(|p| p.contains("isl-dl")));
        assert_eq!(gcc.sha256.len(), 64);
        let binutils = sources
            .get("binutils", crate::spec::DEFAULT_BINUTILS)
            .unwrap();
        assert_eq!(binutils.sha256.len(), 64);
        for (name, version) in GCC_PREREQS {
            assert_eq!(sources.get(name, version).unwrap().sha256.len(), 64);
        }
    }

    #[test]
    fn unknown_component_is_an_error() {
        let sources = ToolchainSources::builtin();
        assert!(matches!(
            sources.get("gcc", "99.0.0"),
            Err(Error::UnknownComponent { .. })
        ));
    }
}
