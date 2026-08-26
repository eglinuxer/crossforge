use serde::Serialize;

use crate::baseline::BaselineRegistry;
use crate::error::{Error, Result};
use crate::target::TargetArch;

/// Default GCC version (decision D2, revised 2026-08-26): the RH
/// gcc-toolset-14 snapshot, carrying the nonshared compat patch series.
pub const DEFAULT_GCC: &str = "14.2.1";
/// Default binutils version (decision D2).
pub const DEFAULT_BINUTILS: &str = "2.40";
/// Default baseline (decision D3).
pub const DEFAULT_BASELINE: &str = "el8";

/// Complete description of one cross toolchain: the input to the
/// `toolchain × sysroot × compat-pack` combination.
///
/// Construct via [`ToolchainSpec::builder`]; `build()` validates against a
/// baseline registry (existence and target-arch support).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ToolchainSpec {
    /// GCC version, e.g. `14.2.1`.
    pub gcc: String,
    /// binutils version, e.g. `2.40`.
    pub binutils: String,
    /// Target architecture.
    pub target: TargetArch,
    /// Baseline alias (must exist in the registry used).
    pub baseline: String,
}

impl ToolchainSpec {
    pub fn builder() -> ToolchainSpecBuilder {
        ToolchainSpecBuilder::default()
    }

    /// Artifact id such as `gcc14.2.1-el8-aarch64`, used for the store's
    /// assembled view and the manifest.
    pub fn id(&self) -> String {
        format!("gcc{}-{}-{}", self.gcc, self.baseline, self.target)
    }
}

#[derive(Debug, Default)]
pub struct ToolchainSpecBuilder {
    gcc: Option<String>,
    binutils: Option<String>,
    target: Option<TargetArch>,
    baseline: Option<String>,
}

impl ToolchainSpecBuilder {
    pub fn gcc(mut self, version: impl Into<String>) -> Self {
        self.gcc = Some(version.into());
        self
    }

    pub fn binutils(mut self, version: impl Into<String>) -> Self {
        self.binutils = Some(version.into());
        self
    }

    pub fn target(mut self, arch: TargetArch) -> Self {
        self.target = Some(arch);
        self
    }

    pub fn baseline(mut self, alias: impl Into<String>) -> Self {
        self.baseline = Some(alias.into());
        self
    }

    /// Applies defaults and validates against the registry: the baseline must
    /// exist and support the target arch.
    pub fn build(self, registry: &BaselineRegistry) -> Result<ToolchainSpec> {
        let spec = ToolchainSpec {
            gcc: self.gcc.unwrap_or_else(|| DEFAULT_GCC.to_string()),
            binutils: self
                .binutils
                .unwrap_or_else(|| DEFAULT_BINUTILS.to_string()),
            target: self.target.unwrap_or(TargetArch::X86_64),
            baseline: self
                .baseline
                .unwrap_or_else(|| DEFAULT_BASELINE.to_string()),
        };
        let Some(def) = registry.get(&spec.baseline) else {
            return Err(Error::UnknownBaseline(spec.baseline));
        };
        if !def.supports(spec.target) {
            return Err(Error::UnsupportedArch {
                baseline: spec.baseline,
                arch: spec.target.to_string(),
            });
        }
        Ok(spec)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_gts14_el8_x86_64() {
        let registry = BaselineRegistry::builtin();
        let spec = ToolchainSpec::builder().build(&registry).unwrap();
        assert_eq!(spec.gcc, "14.2.1");
        assert_eq!(spec.binutils, "2.40");
        assert_eq!(spec.baseline, "el8");
        assert_eq!(spec.target, TargetArch::X86_64);
        assert_eq!(spec.id(), "gcc14.2.1-el8-x86_64");
    }

    #[test]
    fn unknown_baseline_rejected() {
        let registry = BaselineRegistry::builtin();
        let err = ToolchainSpec::builder().baseline("el99").build(&registry);
        assert!(matches!(err, Err(Error::UnknownBaseline(alias)) if alias == "el99"));
    }

    #[test]
    fn unsupported_arch_rejected() {
        let mut registry = BaselineRegistry::builtin();
        registry
            .merge_toml(
                r#"
                [[baseline]]
                alias = "x86only"
                glibc = "2.31"
                kernel_headers = "5.4"
                glibcxx = "3.4.28"
                cxxabi = "1.3.12"
                cxx11_abi = true
                source = "test"
                arches = ["x86_64"]
                "#,
            )
            .unwrap();
        let err = ToolchainSpec::builder()
            .baseline("x86only")
            .target(TargetArch::Aarch64)
            .build(&registry);
        assert!(matches!(err, Err(Error::UnsupportedArch { .. })));
    }
}
