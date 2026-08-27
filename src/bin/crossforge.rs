//! Thin CLI over the crossforge library (feature = "cli").

use std::os::unix::fs::MetadataExt;
use std::path::PathBuf;

use clap::{Parser, Subcommand};
use crossforge::{
    Auditor, BaselineRegistry, BuildConfig, BuildEngine, CheckRunner, CheckSuite, CompilerArtifact,
    ContainerRunner, Fetcher, LocalRunner, Runner, Severity, SourceRegistry, SysrootGenerator,
    TargetArch, ToolchainSpec, pack_toolchain, verify_in_containers, write_manifest,
};

#[derive(Parser)]
#[command(
    name = "crossforge",
    version,
    about = "Cross-compilation toolchain build engine"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Build a complete toolchain (sysroot + binutils + GCC + compat-pack).
    Build {
        #[arg(long, default_value = crossforge::DEFAULT_GCC)]
        gcc: String,
        #[arg(long, default_value = crossforge::DEFAULT_BINUTILS)]
        binutils: String,
        #[arg(long, default_value = crossforge::DEFAULT_BASELINE)]
        baseline: String,
        #[arg(long, default_value = "x86_64")]
        target: String,
        /// Sysroot content profile (minimal / gui / x11 / wayland / qt6);
        /// part of the toolchain id when not minimal.
        #[arg(long, default_value = crossforge::DEFAULT_PROFILE)]
        sysroot_profile: String,
        /// Container image for the build environment (e.g. crossforge-buildenv:el8);
        /// omit to build directly on the host.
        #[arg(long, env = "CROSSFORGE_IMAGE")]
        image: Option<String>,
        #[arg(long, default_value = "/tmp/crossforge")]
        work_dir: PathBuf,
        #[arg(long)]
        jobs: Option<usize>,
        /// Also pack the result into a distributable tar.zst under --out.
        #[arg(long)]
        pack: bool,
        #[arg(long, default_value = "/tmp/crossforge/dist")]
        out: PathBuf,
    },
    /// Generate a baseline sysroot only.
    Sysroot {
        #[arg(long, default_value = crossforge::DEFAULT_BASELINE)]
        baseline: String,
        #[arg(long, default_value = "x86_64")]
        target: String,
        /// Content profile: minimal (curated list) or a resolved one
        /// (gui / x11 / wayland / qt6).
        #[arg(long, default_value = crossforge::DEFAULT_PROFILE)]
        profile: String,
        #[arg(long, default_value = "/tmp/crossforge")]
        work_dir: PathBuf,
        /// Resolve and report the package set without downloading anything.
        #[arg(long)]
        dry_run: bool,
    },
    /// Audit ELF files against a baseline sysroot (exit 1 on errors).
    Audit {
        #[arg(long)]
        sysroot: PathBuf,
        #[arg(long, default_value = "x86_64")]
        arch: String,
        /// Extra DT_NEEDED sonames shipped alongside the binaries.
        #[arg(long = "allow-needed")]
        allow_needed: Vec<String>,
        #[arg(required = true)]
        files: Vec<PathBuf>,
    },
    /// Run the GCC upstream testsuite against a built toolchain.
    Check {
        #[arg(long, default_value = crossforge::DEFAULT_GCC)]
        gcc: String,
        #[arg(long, default_value = crossforge::DEFAULT_BASELINE)]
        baseline: String,
        #[arg(long, default_value = "x86_64")]
        target: String,
        /// Testsuites to run (gcc, c++, libstdc++); default: all.
        #[arg(long, value_delimiter = ',')]
        suites: Vec<String>,
        /// Container image for the test environment (needs dejagnu; qemu-user
        /// for aarch64 targets); omit to run directly on the host.
        #[arg(long, env = "CROSSFORGE_IMAGE")]
        image: Option<String>,
        #[arg(long, default_value = "/tmp/crossforge")]
        work_dir: PathBuf,
        #[arg(long)]
        jobs: Option<usize>,
        /// Exit non-zero if unexpected failures exceed this count.
        #[arg(long)]
        max_unexpected_failures: Option<u64>,
        /// Extra DejaGnu arguments (e.g. `dg.exp` to run one part of a
        /// suite). Repeatable.
        #[arg(long = "runtest-arg")]
        runtest_args: Vec<String>,
    },
    /// Run the built-in toolchain smoke test: compile a dlopen'd plugin with
    /// cross-DSO exception matching of nonshared-provided types, audit the
    /// artifacts, and execute on baseline runtimes (exit 1 on failure).
    Smoke {
        #[arg(long, default_value = crossforge::DEFAULT_GCC)]
        gcc: String,
        #[arg(long, default_value = crossforge::DEFAULT_BASELINE)]
        baseline: String,
        #[arg(long, default_value = "x86_64")]
        target: String,
        /// Container images to execute on (x86_64 targets only).
        #[arg(
            long,
            value_delimiter = ',',
            default_value = "rockylinux:8,ubuntu:20.04,debian:11"
        )]
        images: Vec<String>,
        #[arg(long, default_value = "/tmp/crossforge")]
        work_dir: PathBuf,
    },
    /// Build relocatable CPython packs (design doc §9, M6): a native x86_64
    /// build per version (doubling as the build-python), then cross builds
    /// per target arch, each followed by an import smoke test.
    Python {
        #[arg(long, default_value = crossforge::DEFAULT_GCC)]
        gcc: String,
        /// Baseline (manylinux_2_28 only, i.e. el8).
        #[arg(long, default_value = crossforge::PYTHON_BASELINE)]
        baseline: String,
        /// Target arches; x86_64 is always built (it is the build-python).
        #[arg(long, value_delimiter = ',', default_value = "x86_64,aarch64")]
        targets: Vec<String>,
        /// Full CPython versions (e.g. 3.12.14); default: all built-in.
        #[arg(long, value_delimiter = ',')]
        versions: Vec<String>,
        /// Container image for the build environment; omit to build on the host.
        #[arg(long, env = "CROSSFORGE_IMAGE")]
        image: Option<String>,
        #[arg(long, default_value = "/tmp/crossforge")]
        work_dir: PathBuf,
        #[arg(long)]
        jobs: Option<usize>,
        /// Fetch prebuilt packs from published images instead of building
        /// CPython locally (no toolchain needed for x86_64).
        #[arg(long)]
        pull: bool,
        /// Registry repository holding the pack images (with --pull).
        #[arg(long, default_value = "ghcr.io/eglinuxer/crossforge/python")]
        registry: String,
        /// Image tag suffix, e.g. a commit sha (with --pull).
        #[arg(long)]
        image_ref: Option<String>,
        /// Also pack each result into a distributable tar.zst under --out.
        #[arg(long)]
        pack: bool,
        #[arg(long, default_value = "/tmp/crossforge/dist")]
        out: PathBuf,
    },
    /// Build manylinux_2_28 wheels for a project across the full CPython x
    /// arch matrix (design doc §9, M7): PEP 517 build via the python packs,
    /// policy audit, retag, import smoke (qemu for aarch64), and optional
    /// verification inside the official manylinux containers.
    Wheel {
        /// Project directory (pyproject.toml / setup.py).
        project: PathBuf,
        #[arg(long, default_value = crossforge::DEFAULT_GCC)]
        gcc: String,
        /// Baseline (manylinux_2_28 only, i.e. el8).
        #[arg(long, default_value = crossforge::PYTHON_BASELINE)]
        baseline: String,
        #[arg(long, value_delimiter = ',', default_value = "x86_64,aarch64")]
        targets: Vec<String>,
        /// Full CPython versions (e.g. 3.12.14); default: all built-in.
        #[arg(long, value_delimiter = ',')]
        versions: Vec<String>,
        /// Container image for the build environment; omit to build on the host.
        #[arg(long, env = "CROSSFORGE_IMAGE")]
        image: Option<String>,
        #[arg(long, default_value = "/tmp/crossforge")]
        work_dir: PathBuf,
        /// Output directory for the final wheels.
        #[arg(long, default_value = "/tmp/crossforge/wheels")]
        out: PathBuf,
        /// Also import-check every wheel inside the official manylinux_2_28
        /// container for its arch (aarch64 needs binfmt_misc qemu).
        #[arg(long)]
        verify_manylinux: bool,
        /// Additional container images for the install-layer check (distro
        /// sampling); images without a matching interpreter are skipped.
        #[arg(long, value_delimiter = ',')]
        verify_images: Vec<String>,
        /// Extra directories holding target-arch libraries to vendor from
        /// (the toolchain sysroot is always searched).
        #[arg(long)]
        vendor_path: Vec<PathBuf>,
        /// Sonames the runtime provides (e.g. libcuda.so.1): neither
        /// vendored nor flagged by the audit.
        #[arg(long)]
        exclude: Vec<String>,
    },
    /// Run a binary across distro container images (exit 1 on failures).
    Verify {
        binary: PathBuf,
        #[arg(long, required = true, value_delimiter = ',')]
        images: Vec<String>,
        #[arg(long, default_value = "docker")]
        engine: String,
        #[arg(long, default_value = "/tmp/crossforge/verify-logs")]
        log_dir: PathBuf,
    },
}

fn main() -> crossforge::Result<()> {
    tracing_subscriber::fmt()
        .with_max_level(tracing::Level::INFO)
        .init();
    match Cli::parse().command {
        Command::Build {
            gcc,
            binutils,
            baseline,
            target,
            sysroot_profile,
            image,
            work_dir,
            jobs,
            pack,
            out,
        } => {
            let registry = BaselineRegistry::builtin();
            let spec = ToolchainSpec::builder()
                .gcc(gcc)
                .binutils(binutils)
                .baseline(baseline)
                .target(target.parse::<TargetArch>()?)
                .sysroot_profile(sysroot_profile)
                .build(&registry)?;
            let config = BuildConfig {
                cache_dir: work_dir.join("cache"),
                work_dir,
                mirror: None,
                jobs,
            };
            let artifact = match image {
                Some(image) => {
                    let me = std::fs::metadata("/proc/self")?;
                    let runner = ContainerRunner {
                        engine: "docker".to_string(),
                        image,
                        binds: vec![config.work_dir.clone()],
                        user: Some(format!("{}:{}", me.uid(), me.gid())),
                    };
                    BuildEngine::with_runner(config, runner).build(&spec, &registry)?
                }
                None => BuildEngine::new(config).build(&spec, &registry)?,
            };
            println!("toolchain: {}", artifact.root.display());
            if pack {
                let packed =
                    pack_toolchain(&artifact, "x86_64-linux", &out, &crossforge::LocalRunner)?;
                write_manifest(&out)?;
                println!("tarball:   {}", packed.tarball.display());
            }
        }
        Command::Sysroot {
            baseline,
            target,
            profile,
            work_dir,
            dry_run,
        } => {
            let registry = BaselineRegistry::builtin();
            let sources = SourceRegistry::builtin();
            let def = registry
                .get(&baseline)
                .ok_or_else(|| crossforge::Error::UnknownBaseline(baseline.clone()))?;
            let arch = target.parse::<TargetArch>()?;
            let expanded = sources.profile(&profile)?;
            let fetcher = Fetcher::new(work_dir.join("cache"))?;
            let generator = SysrootGenerator::new(&fetcher, &sources, None);
            if dry_run {
                let plan = generator.plan(def, arch, &expanded)?;
                println!(
                    "profile {profile}: {} packages resolved",
                    plan.packages.len()
                );
                for (_, pkg) in &plan.packages {
                    println!("  {} {} ({})", pkg.name, pkg.evr(), pkg.arch);
                }
                if !plan.outcome.missing_seeds.is_empty() {
                    println!(
                        "\nMISSING SEEDS ({}): {}",
                        plan.outcome.missing_seeds.len(),
                        plan.outcome.missing_seeds.join(" ")
                    );
                }
                if !plan.outcome.unresolved.is_empty() {
                    println!(
                        "\nunsatisfied capabilities ({}): {}",
                        plan.outcome.unresolved.len(),
                        plan.outcome.unresolved.join(" ")
                    );
                }
                if !plan.outcome.missing_seeds.is_empty() {
                    std::process::exit(1);
                }
                return Ok(());
            }
            let sysroot_id = if profile == crossforge::DEFAULT_PROFILE {
                format!("{baseline}-{target}")
            } else {
                format!("{baseline}-{profile}-{target}")
            };
            let out = work_dir.join("sysroots").join(sysroot_id);
            let artifact = generator.generate(def, arch, &expanded, &out)?;
            println!("sysroot: {}", artifact.root.display());
        }
        Command::Audit {
            sysroot,
            arch,
            allow_needed,
            files,
        } => {
            let mut auditor = Auditor::from_sysroot(&sysroot, arch.parse::<TargetArch>()?)?;
            for soname in allow_needed {
                auditor.allow_needed(soname);
            }
            let mut failed = false;
            for path in &files {
                let report = auditor.audit_file(path)?;
                if report.findings.is_empty() {
                    println!("PASS {}", path.display());
                }
                for f in &report.findings {
                    let tag = match f.severity {
                        Severity::Error => "ERROR",
                        Severity::Warning => "WARN ",
                    };
                    println!("{tag} {} [{}] {}", path.display(), f.check, f.message);
                }
                failed |= !report.passed();
            }
            if failed {
                std::process::exit(1);
            }
        }
        Command::Check {
            gcc,
            baseline,
            target,
            suites,
            image,
            work_dir,
            jobs,
            max_unexpected_failures,
            runtest_args,
        } => {
            let registry = BaselineRegistry::builtin();
            let spec = ToolchainSpec::builder()
                .gcc(gcc)
                .baseline(baseline)
                .target(target.parse::<TargetArch>()?)
                .build(&registry)?;
            let suites: Vec<CheckSuite> = if suites.is_empty() {
                CheckSuite::ALL.to_vec()
            } else {
                suites
                    .iter()
                    .map(|s| s.parse())
                    .collect::<crossforge::Result<_>>()?
            };
            let prefix = work_dir.join("toolchains").join(spec.id());
            let compiler = CompilerArtifact {
                prefix,
                triple: spec.target.triple(),
            };
            let jobs = jobs.unwrap_or_else(|| {
                std::thread::available_parallelism()
                    .map(|n| n.get())
                    .unwrap_or(4)
            });
            let summaries = match image {
                Some(image) => {
                    let me = std::fs::metadata("/proc/self")?;
                    let runner = ContainerRunner {
                        engine: "docker".to_string(),
                        image,
                        binds: vec![work_dir.clone()],
                        user: Some(format!("{}:{}", me.uid(), me.gid())),
                    };
                    run_check(
                        &runner,
                        work_dir,
                        jobs,
                        &spec,
                        &compiler,
                        &suites,
                        &runtest_args,
                    )?
                }
                None => run_check(
                    &LocalRunner,
                    work_dir,
                    jobs,
                    &spec,
                    &compiler,
                    &suites,
                    &runtest_args,
                )?,
            };
            let mut over_limit = false;
            for s in &summaries {
                println!(
                    "{:>10}: {} passes, {} unexpected failures, {} expected failures, {} unresolved, {} unsupported",
                    s.suite,
                    s.expected_passes,
                    s.unexpected_failures,
                    s.expected_failures,
                    s.unresolved,
                    s.unsupported
                );
                if let Some(max) = max_unexpected_failures {
                    over_limit |= s.unexpected_failures > max;
                }
            }
            if over_limit {
                std::process::exit(1);
            }
        }
        Command::Smoke {
            gcc,
            baseline,
            target,
            images,
            work_dir,
        } => {
            let registry = BaselineRegistry::builtin();
            let spec = ToolchainSpec::builder()
                .gcc(gcc)
                .baseline(baseline)
                .target(target.parse::<TargetArch>()?)
                .build(&registry)?;
            let prefix = work_dir.join("toolchains").join(spec.id());
            let runner = crossforge::LocalRunner;
            let outcome = crossforge::SmokeRunner {
                runner: &runner,
                work_dir: work_dir.clone(),
            }
            .run(&spec, &prefix, &images)?;
            for r in &outcome.runs {
                println!("{} {}", if r.passed { "PASS" } else { "FAIL" }, r.image);
            }
            if !outcome.passed() {
                std::process::exit(1);
            }
        }
        Command::Python {
            gcc,
            baseline,
            targets,
            versions,
            image,
            work_dir,
            jobs,
            pull,
            registry,
            image_ref,
            pack,
            out,
        } => {
            if baseline != crossforge::PYTHON_BASELINE {
                return Err(crossforge::Error::PythonPack(format!(
                    "python packs support only the manylinux_2_28 baseline (el8), got `{baseline}`"
                )));
            }
            let baselines = BaselineRegistry::builtin();
            let mut arches: Vec<TargetArch> = vec![TargetArch::X86_64];
            for t in &targets {
                let arch = t.parse::<TargetArch>()?;
                if !arches.contains(&arch) {
                    arches.push(arch);
                }
            }
            // Toolchains are required to build packs; when pulling they are
            // only needed for the qemu smoke of foreign-arch packs.
            let mut prefixes = std::collections::BTreeMap::new();
            for arch in &arches {
                let spec = ToolchainSpec::builder()
                    .gcc(gcc.clone())
                    .baseline(baseline.clone())
                    .target(*arch)
                    .build(&baselines)?;
                let prefix = work_dir.join("toolchains").join(spec.id());
                if prefix.join("bin").is_dir() {
                    prefixes.insert(*arch, prefix);
                } else if !pull {
                    return Err(crossforge::Error::PythonPack(format!(
                        "toolchain {} not found under {} (run `crossforge build` first)",
                        spec.id(),
                        prefix.display()
                    )));
                }
            }
            let versions: Vec<String> = if versions.is_empty() {
                crossforge::PYTHON_VERSIONS
                    .iter()
                    .map(|v| v.to_string())
                    .collect()
            } else {
                versions
            };
            let fetcher = Fetcher::new(work_dir.join("cache"))?;
            let jobs = jobs.unwrap_or_else(|| {
                std::thread::available_parallelism()
                    .map(|n| n.get())
                    .unwrap_or(4)
            });
            let out_root = work_dir.join("python-packs");
            let run = |runner: &dyn Runner| -> crossforge::Result<Vec<crossforge::PythonPack>> {
                let builder = crossforge::PythonBuilder {
                    fetcher: &fetcher,
                    runner: &runner,
                    sources: crossforge::ToolchainSources::builtin(),
                    work_dir: work_dir.clone(),
                    jobs,
                };
                let mut packs = Vec::new();
                for version in &versions {
                    if pull {
                        for arch in &arches {
                            let pack = crossforge::pull_pack(
                                "docker",
                                &registry,
                                version,
                                *arch,
                                image_ref.as_deref(),
                                &out_root,
                            )?;
                            let toolchain = prefixes.get(arch).map(PathBuf::as_path);
                            if *arch == TargetArch::X86_64 || toolchain.is_some() {
                                builder.smoke(&pack, toolchain)?;
                            } else {
                                tracing::warn!(
                                    arch = %arch,
                                    "no toolchain for the qemu sysroot; skipping import smoke"
                                );
                            }
                            packs.push(pack);
                        }
                        continue;
                    }
                    let native = builder.build(
                        version,
                        TargetArch::X86_64,
                        &prefixes[&TargetArch::X86_64],
                        None,
                        &out_root,
                    )?;
                    builder.smoke(&native, Some(&prefixes[&TargetArch::X86_64]))?;
                    packs.push(native.clone());
                    for arch in arches.iter().filter(|a| **a != TargetArch::X86_64) {
                        let pack = builder.build(
                            version,
                            *arch,
                            &prefixes[arch],
                            Some(&native),
                            &out_root,
                        )?;
                        builder.smoke(&pack, Some(&prefixes[arch]))?;
                        packs.push(pack);
                    }
                }
                Ok(packs)
            };
            let packs = match image {
                Some(image) => {
                    let me = std::fs::metadata("/proc/self")?;
                    let runner = ContainerRunner {
                        engine: "docker".to_string(),
                        image,
                        binds: vec![work_dir.clone()],
                        user: Some(format!("{}:{}", me.uid(), me.gid())),
                    };
                    run(&runner)?
                }
                None => run(&LocalRunner)?,
            };
            for p in &packs {
                println!("python pack: {}", p.root.display());
            }
            if pack {
                for p in &packs {
                    let (tarball, _) = crossforge::pack_python(p, &baseline, &out, &LocalRunner)?;
                    println!("tarball:     {}", tarball.display());
                }
                write_manifest(&out)?;
            }
        }
        Command::Wheel {
            project,
            gcc,
            baseline,
            targets,
            versions,
            image,
            work_dir,
            out,
            verify_manylinux,
            verify_images,
            vendor_path,
            exclude,
        } => {
            if baseline != crossforge::PYTHON_BASELINE {
                return Err(crossforge::Error::Wheel(format!(
                    "wheels support only the manylinux_2_28 baseline (el8), got `{baseline}`"
                )));
            }
            let project = project.canonicalize()?;
            let registry = BaselineRegistry::builtin();
            let arches: Vec<TargetArch> = targets
                .iter()
                .map(|t| t.parse::<TargetArch>())
                .collect::<crossforge::Result<_>>()?;
            let mut prefixes = std::collections::BTreeMap::new();
            for arch in &arches {
                let spec = ToolchainSpec::builder()
                    .gcc(gcc.clone())
                    .baseline(baseline.clone())
                    .target(*arch)
                    .build(&registry)?;
                let prefix = work_dir.join("toolchains").join(spec.id());
                if !prefix.join("bin").is_dir() {
                    return Err(crossforge::Error::Wheel(format!(
                        "toolchain {} not found under {} (run `crossforge build` first)",
                        spec.id(),
                        prefix.display()
                    )));
                }
                prefixes.insert(*arch, prefix);
            }
            let versions: Vec<String> = if versions.is_empty() {
                crossforge::PYTHON_VERSIONS
                    .iter()
                    .map(|v| v.to_string())
                    .collect()
            } else {
                versions
            };
            let packs_root = work_dir.join("python-packs");
            let fetcher = Fetcher::new(work_dir.join("cache"))?;
            let run = |runner: &dyn Runner| -> crossforge::Result<Vec<crossforge::WheelArtifact>> {
                let builder = crossforge::WheelBuilder {
                    fetcher: &fetcher,
                    runner: &runner,
                    sources: crossforge::ToolchainSources::builtin(),
                    work_dir: work_dir.clone(),
                    policy: crossforge::WheelPolicy::builtin(),
                    vendor_paths: vendor_path.clone(),
                    exclude: exclude.clone(),
                };
                let mut artifacts = Vec::new();
                let mut abi3_done: std::collections::BTreeSet<TargetArch> =
                    std::collections::BTreeSet::new();
                for version in &versions {
                    if !crossforge::project_supports_python(&project, version)? {
                        println!("skip cpython {version}: outside the project's requires-python");
                        continue;
                    }
                    let native =
                        crossforge::PythonPack::open(&packs_root, version, TargetArch::X86_64)?;
                    for arch in &arches {
                        if abi3_done.contains(arch) {
                            continue;
                        }
                        let target_pack = if *arch == TargetArch::X86_64 {
                            native.clone()
                        } else {
                            crossforge::PythonPack::open(&packs_root, version, *arch)?
                        };
                        let artifact = builder.build(&crossforge::WheelRequest {
                            project_dir: &project,
                            python_version: version,
                            arch: *arch,
                            toolchain_prefix: &prefixes[arch],
                            native_pack: &native,
                            target_pack: &target_pack,
                            out_dir: &out,
                        })?;
                        // abi3 wheels: one build covers every version; the
                        // smoke fans out across all requested interpreters.
                        let smoke_packs: Vec<crossforge::PythonPack> = if artifact.is_abi3() {
                            abi3_done.insert(*arch);
                            versions
                                .iter()
                                .map(|v| crossforge::PythonPack::open(&packs_root, v, *arch))
                                .collect::<crossforge::Result<_>>()?
                        } else {
                            vec![target_pack]
                        };
                        let refs: Vec<&crossforge::PythonPack> = smoke_packs.iter().collect();
                        builder.smoke(&artifact, &refs, &prefixes[arch])?;
                        let mut install_images = Vec::new();
                        if verify_manylinux {
                            install_images
                                .push(format!("quay.io/pypa/manylinux_2_28_{}", arch.as_str()));
                        }
                        install_images.extend(verify_images.iter().cloned());
                        if !install_images.is_empty() {
                            builder.verify_in_images(&artifact, &install_images, "docker")?;
                        }
                        artifacts.push(artifact);
                    }
                }
                Ok(artifacts)
            };
            let artifacts = match image {
                Some(image) => {
                    let me = std::fs::metadata("/proc/self")?;
                    let runner = ContainerRunner {
                        engine: "docker".to_string(),
                        image,
                        // The project tree and output dir may live outside
                        // the work dir; all three must be visible inside.
                        binds: vec![work_dir.clone(), project.clone(), out.clone()],
                        user: Some(format!("{}:{}", me.uid(), me.gid())),
                    };
                    run(&runner)?
                }
                None => run(&LocalRunner)?,
            };
            for a in &artifacts {
                if a.vendored.is_empty() {
                    println!("wheel: {}", a.path.display());
                } else {
                    println!(
                        "wheel: {} (vendored: {})",
                        a.path.display(),
                        a.vendored.join(", ")
                    );
                }
            }
        }
        Command::Verify {
            binary,
            images,
            engine,
            log_dir,
        } => {
            let binary = binary.canonicalize()?;
            let binds = vec![binary.parent().unwrap().to_path_buf()];
            let results = verify_in_containers(&engine, &images, &binary, &[], &binds, &log_dir)?;
            let mut failed = false;
            for r in &results {
                println!("{} {}", if r.passed { "PASS" } else { "FAIL" }, r.image);
                failed |= !r.passed;
            }
            if failed {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn run_check(
    runner: &impl Runner,
    work_dir: PathBuf,
    jobs: usize,
    spec: &ToolchainSpec,
    compiler: &CompilerArtifact,
    suites: &[CheckSuite],
    runtest_args: &[String],
) -> crossforge::Result<Vec<crossforge::CheckSummary>> {
    CheckRunner {
        runner,
        work_dir,
        jobs,
        runtest_args: runtest_args.to_vec(),
    }
    .run(spec, compiler, suites)
}
