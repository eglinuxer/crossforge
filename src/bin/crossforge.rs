//! Thin CLI over the crossforge library (feature = "cli").

use std::os::unix::fs::MetadataExt;
use std::path::PathBuf;

use clap::{Parser, Subcommand};
use crossforge::{
    Auditor, BaselineRegistry, BuildConfig, BuildEngine, ContainerRunner, Fetcher, Severity,
    SourceRegistry, SysrootGenerator, TargetArch, ToolchainSpec, pack_toolchain,
    verify_in_containers, write_manifest,
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
        #[arg(long, default_value = "/tmp/crossforge")]
        work_dir: PathBuf,
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
            work_dir,
        } => {
            let registry = BaselineRegistry::builtin();
            let sources = SourceRegistry::builtin();
            let def = registry
                .get(&baseline)
                .ok_or_else(|| crossforge::Error::UnknownBaseline(baseline.clone()))?;
            let fetcher = Fetcher::new(work_dir.join("cache"))?;
            let out = work_dir
                .join("sysroots")
                .join(format!("{baseline}-{target}"));
            let artifact = SysrootGenerator::new(&fetcher, &sources, None).generate(
                def,
                target.parse::<TargetArch>()?,
                &out,
            )?;
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
