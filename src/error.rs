use thiserror::Error;

/// Unified error type for the crate.
///
/// `#[non_exhaustive]`: later milestones (compiler/compat/audit) will add
/// variants, so callers must keep a wildcard arm when matching.
#[derive(Debug, Error)]
#[non_exhaustive]
pub enum Error {
    #[error("unknown baseline alias: {0}")]
    UnknownBaseline(String),

    #[error("baseline `{baseline}` does not support target arch `{arch}`")]
    UnsupportedArch { baseline: String, arch: String },

    #[error("duplicate baseline alias in registry: {0}")]
    DuplicateBaseline(String),

    #[error("failed to parse registry TOML: {0}")]
    Registry(#[from] toml::de::Error),

    #[error("failed to serialize metadata TOML: {0}")]
    Serialize(#[from] toml::ser::Error),

    #[error("unknown target arch: {0}")]
    UnknownArch(String),

    #[error("unknown package source: {0}")]
    UnknownSource(String),

    #[error("duplicate package source id: {0}")]
    DuplicateSource(String),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("command `{program}` exited with status {status}")]
    CommandFailed {
        program: String,
        status: std::process::ExitStatus,
    },

    #[error("http request failed for {url}: {reason}")]
    Http { url: String, reason: String },

    #[error("checksum mismatch for {what}: expected {expected}, got {actual}")]
    ChecksumMismatch {
        what: String,
        expected: String,
        actual: String,
    },

    #[error("package `{name}` ({arch}) not found in configured repos")]
    PackageNotFound { name: String, arch: String },

    #[error("unknown source component: {name} {version}")]
    UnknownComponent { name: String, version: String },

    #[error("unknown testsuite: {0} (expected gcc, c++ or libstdc++)")]
    UnknownSuite(String),

    #[error("smoke test artifact {artifact} failed the baseline audit: {details}")]
    SmokeAudit { artifact: String, details: String },

    #[error("python pack: {0}")]
    PythonPack(String),

    #[error("malformed repo metadata: {0}")]
    RepoMetadata(String),

    #[error("malformed rpm: {0}")]
    Rpm(String),

    #[error("malformed cpio archive: {0}")]
    Cpio(String),

    #[error("unsupported payload compression: {0}")]
    UnsupportedCompression(String),

    #[error("malformed elf: {0}")]
    Elf(String),

    #[error("malformed ar archive: {0}")]
    Archive(String),

    /// Pipeline stage not implemented yet, tagged with its design-doc milestone.
    #[error("not implemented yet ({milestone}): {what}")]
    Unimplemented {
        milestone: &'static str,
        what: &'static str,
    },
}

pub type Result<T> = std::result::Result<T, Error>;
