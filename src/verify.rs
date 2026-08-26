//! Real-machine smoke verification (design doc §5.5): run built artifacts in
//! a matrix of distro containers to catch anything the static audit missed.

use std::path::{Path, PathBuf};

use crate::engine::{Cmd, ContainerRunner, Runner};
use crate::error::Result;

/// The outcome of running one binary in one distro image.
#[derive(Debug, Clone)]
pub struct VerifyResult {
    pub image: String,
    pub passed: bool,
    pub log: PathBuf,
}

/// Runs `program` (with `args`) inside each image of the matrix, bind-mounting
/// `binds` at identical paths. A non-zero exit or engine failure marks the
/// image as failed; per-image logs land in `log_dir`.
pub fn verify_in_containers(
    engine: &str,
    images: &[String],
    program: &Path,
    args: &[String],
    binds: &[PathBuf],
    log_dir: &Path,
) -> Result<Vec<VerifyResult>> {
    std::fs::create_dir_all(log_dir)?;
    let mut results = Vec::new();
    for image in images {
        let log = log_dir.join(format!("verify-{}.log", image.replace(['/', ':'], "-")));
        let runner = ContainerRunner {
            engine: engine.to_string(),
            image: image.clone(),
            binds: binds.to_vec(),
            user: None,
        };
        let cmd = Cmd::new(program.display().to_string())
            .args(args.iter().cloned())
            .log(&log);
        let passed = match runner.exec(&cmd) {
            Ok(()) => true,
            Err(e) => {
                tracing::warn!(image = %image, error = %e, "verify failed");
                false
            }
        };
        tracing::info!(image = %image, passed, "verify");
        results.push(VerifyResult {
            image: image.clone(),
            passed,
            log,
        });
    }
    Ok(results)
}
