use std::io::Read;
use std::path::PathBuf;

use sha2::{Digest, Sha256};

use crate::error::{Error, Result};

pub(crate) fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

pub(crate) fn sha256_hex(data: &[u8]) -> String {
    hex(&Sha256::digest(data))
}

/// Downloader with a content-addressed cache.
///
/// Files with a known sha256 land in the cache as `sha256-<hex>` and are
/// trusted by name on later runs; files without one (e.g. `repomd.xml`, which
/// must stay fresh) are fetched every time.
#[derive(Debug)]
pub struct Fetcher {
    cache_dir: PathBuf,
}

impl Fetcher {
    pub fn new(cache_dir: PathBuf) -> Result<Self> {
        std::fs::create_dir_all(&cache_dir)?;
        Ok(Self { cache_dir })
    }

    fn download(url: &str) -> Result<Vec<u8>> {
        let response = ureq::get(url).call().map_err(|e| Error::Http {
            url: url.to_string(),
            reason: e.to_string(),
        })?;
        let mut data = Vec::new();
        response
            .into_reader()
            .read_to_end(&mut data)
            .map_err(|e| Error::Http {
                url: url.to_string(),
                reason: e.to_string(),
            })?;
        Ok(data)
    }

    /// Fetches `url` without caching (for small, must-be-fresh metadata).
    pub fn fetch_bytes(&self, url: &str) -> Result<Vec<u8>> {
        tracing::debug!(url, "fetching (uncached)");
        Self::download(url)
    }

    /// Fetches `url` into the cache, verifying it against `sha256`.
    /// Returns the cached file path; a pre-existing cache entry skips the
    /// download entirely.
    pub fn fetch_cached(&self, url: &str, sha256: &str) -> Result<PathBuf> {
        let path = self.cache_dir.join(format!("sha256-{sha256}"));
        if path.is_file() {
            tracing::debug!(url, "cache hit");
            return Ok(path);
        }
        tracing::info!(url, "downloading");
        let data = Self::download(url)?;
        let actual = sha256_hex(&data);
        if actual != sha256 {
            return Err(Error::ChecksumMismatch {
                what: url.to_string(),
                expected: sha256.to_string(),
                actual,
            });
        }
        // Write via a temp name so a concurrent or interrupted run never
        // leaves a partial file under the final content-addressed name.
        let tmp = self
            .cache_dir
            .join(format!(".tmp-{sha256}-{}", std::process::id()));
        std::fs::write(&tmp, &data)?;
        std::fs::rename(&tmp, &path)?;
        Ok(path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_encoding() {
        assert_eq!(hex(&[0x00, 0xff, 0x1a]), "00ff1a");
    }

    #[test]
    fn sha256_known_vector() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
