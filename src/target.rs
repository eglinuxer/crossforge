use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

use crate::error::Error;

/// Vendor field of the target triple. The conventional generic vendor is
/// used so tool names look like any standard cross toolchain (design doc §3.1).
pub const VENDOR: &str = "unknown";

/// Target CPU architecture.
///
/// `#[non_exhaustive]`: decision D4 reserves room for more architectures
/// (LoongArch, ...), so adding one is not a breaking change.
#[non_exhaustive]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TargetArch {
    X86_64,
    Aarch64,
}

impl TargetArch {
    /// Architecture name (as in `uname -m` / RPM arch).
    pub fn as_str(&self) -> &'static str {
        match self {
            TargetArch::X86_64 => "x86_64",
            TargetArch::Aarch64 => "aarch64",
        }
    }

    /// Full target triple, e.g. `aarch64-unknown-linux-gnu`.
    pub fn triple(&self) -> String {
        format!("{}-{VENDOR}-linux-gnu", self.as_str())
    }

    /// The glibc dynamic-loader path expected in PT_INTERP.
    pub fn interp(&self) -> &'static str {
        match self {
            TargetArch::X86_64 => "/lib64/ld-linux-x86-64.so.2",
            TargetArch::Aarch64 => "/lib/ld-linux-aarch64.so.1",
        }
    }

    /// ELF `e_machine` value for this architecture.
    pub fn e_machine(&self) -> u16 {
        match self {
            TargetArch::X86_64 => 62,   // EM_X86_64
            TargetArch::Aarch64 => 183, // EM_AARCH64
        }
    }
}

impl fmt::Display for TargetArch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for TargetArch {
    type Err = Error;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "x86_64" => Ok(TargetArch::X86_64),
            "aarch64" | "arm64" => Ok(TargetArch::Aarch64),
            other => Err(Error::UnknownArch(other.to_string())),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn triple_uses_generic_vendor() {
        assert_eq!(TargetArch::X86_64.triple(), "x86_64-unknown-linux-gnu");
        assert_eq!(TargetArch::Aarch64.triple(), "aarch64-unknown-linux-gnu");
    }

    #[test]
    fn parse_arch() {
        assert_eq!("x86_64".parse::<TargetArch>().unwrap(), TargetArch::X86_64);
        assert_eq!("arm64".parse::<TargetArch>().unwrap(), TargetArch::Aarch64);
        assert!("riscv64".parse::<TargetArch>().is_err());
    }
}
