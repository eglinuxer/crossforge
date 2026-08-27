//! crossforge — a cross-compilation toolchain build engine.
//!
//! Solves the glibc / libstdc++ compatibility problem for C/C++ binaries that
//! must run on older Linux distributions: an old-glibc binary sysroot pins the
//! `GLIBC_*` baseline, and libstdc++ nonshared hybrid linking (a cross-compiled
//! take on RH devtoolset's mechanism) pins the `GLIBCXX_*` baseline. Driven by
//! a spec, it builds relocatable toolchains for any
//! (compiler × target × baseline) combination.
//!
//! See `docs/crossforge-design.md` for the design document (in Chinese).
//!
//! # Quick start
//!
//! Generate an el8 sysroot (milestone M1; the full compiler pipeline lands in
//! M2/M3):
//!
//! ```no_run
//! use crossforge::{BaselineRegistry, Fetcher, SourceRegistry, SysrootGenerator, TargetArch};
//!
//! # fn main() -> crossforge::Result<()> {
//! let baselines = BaselineRegistry::builtin();
//! let sources = SourceRegistry::builtin();
//! let fetcher = Fetcher::new("/build/cache".into())?;
//! let generator = SysrootGenerator::new(&fetcher, &sources, None);
//! let sysroot = generator.generate(
//!     baselines.get("el8").unwrap(),
//!     TargetArch::X86_64,
//!     "/build/sysroots/el8-x86_64".as_ref(),
//! )?;
//! println!("sysroot at {}", sysroot.root.display());
//! # Ok(())
//! # }
//! ```

mod ar;
mod audit;
mod baseline;
mod check;
mod compat;
mod compiler;
mod elfdyn;
mod elfpatch;
mod engine;
mod error;
mod fetch;
mod pack;
mod python;
mod repodata;
mod rpm;
mod smoke;
mod source;
mod spec;
mod sysroot;
mod target;
mod vendor;
mod verify;
mod wheel;
mod wheelaudit;
mod whl;

pub use audit::{AuditReport, Auditor, Finding, Severity};
pub use baseline::{BaselineDef, BaselineRegistry};
pub use check::{CheckRunner, CheckSuite, CheckSummary};
pub use compat::{CompatArtifact, CompatBuilder, NonsharedSource};
pub use compiler::{
    CompilerArtifact, CompilerBuilder, ComponentDef, ComponentKind, ToolchainSources,
};
pub use elfdyn::{
    DynSymbol, ElfInfo, VersionNeed, defined_global_symbols, exported_symbols, inspect,
    render_abilist,
};
pub use elfpatch::{PatchOps, patch_elf, read_runpath};
pub use engine::{
    BuildConfig, BuildEngine, Cmd, ContainerRunner, LocalRunner, Runner, ToolchainArtifact,
};
pub use error::{Error, Result};
pub use fetch::Fetcher;
pub use pack::{BundleEntry, PackedToolchain, pack_toolchain, write_manifest};
pub use python::{
    PYTHON_BASELINE, PYTHON_VERSIONS, PythonBuilder, PythonPack, PythonPackMetadata, pack_tag,
};
pub use smoke::{SmokeOutcome, SmokeRunner};
pub use source::{SourceDef, SourceRegistry};
pub use spec::{
    DEFAULT_BASELINE, DEFAULT_BINUTILS, DEFAULT_GCC, ToolchainSpec, ToolchainSpecBuilder,
};
pub use sysroot::{PackageRecord, SysrootArtifact, SysrootGenerator, SysrootMetadata};
pub use target::{TargetArch, VENDOR};
pub use vendor::{VendoredLib, vendor_wheel};
pub use verify::{VerifyResult, verify_in_containers};
pub use wheel::{PIP_VERSION, WheelArtifact, WheelBuilder, WheelRequest, project_supports_python};
pub use wheelaudit::{WheelPolicy, audit_wheel};
pub use whl::{WheelEntry, WheelName, read_wheel, retag_platform, verify_record, write_wheel};
