use std::io::Read;
use std::path::PathBuf;
use std::time::Duration;

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

/// Whether an HTTP status is worth asking about again: the server saying it
/// is overloaded (429) or broken (5xx), as opposed to saying the request
/// itself is wrong.
fn is_retryable_status(code: u16) -> bool {
    code == 429 || (500..600).contains(&code)
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

    /// Downloads `url`, retrying failures that a second attempt could fix.
    ///
    /// One reset connection used to fail an entire toolchain build. Every
    /// RPM and source tarball comes through here, and the GNU mirrors
    /// intermittently refuse or drop connections from CI runners — three of
    /// four jobs in one run died on the same gdb tarball, with
    /// `Connection reset by peer` and `Network is unreachable`.
    fn download(url: &str) -> Result<Vec<u8>> {
        const ATTEMPTS: u32 = 4;
        let mut attempt = 1;
        loop {
            match Self::download_once(url) {
                Ok(data) => return Ok(data),
                Err((reason, retryable)) if retryable && attempt < ATTEMPTS => {
                    let backoff = Duration::from_secs(1 << (attempt - 1));
                    tracing::warn!(url, attempt, ?backoff, reason, "download failed; retrying");
                    std::thread::sleep(backoff);
                    attempt += 1;
                }
                Err((reason, _)) => {
                    return Err(Error::Http {
                        url: url.to_string(),
                        reason,
                    });
                }
            }
        }
    }

    /// One attempt. The flag says whether retrying could plausibly help.
    fn download_once(url: &str) -> std::result::Result<Vec<u8>, (String, bool)> {
        let response = match ureq::get(url).call() {
            Ok(response) => response,
            // A status code is the server's considered answer, so most of
            // them mean the same thing next time; a 404 will not improve
            // with waiting.
            Err(ureq::Error::Status(code, _)) => {
                return Err((format!("HTTP {code}"), is_retryable_status(code)));
            }
            // Everything else is transport-level: DNS, TLS, refused or reset
            // connections.
            Err(e) => return Err((e.to_string(), true)),
        };
        let mut data = Vec::new();
        // A body that stops early is a dropped connection, not a bad URL.
        response
            .into_reader()
            .read_to_end(&mut data)
            .map_err(|e| (e.to_string(), true))?;
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

    /// Fetches the first of `urls` that answers, verifying against `sha256`.
    ///
    /// Mirrors cover what retries cannot. Retrying helps when a connection
    /// dropped; it does nothing when a host is refusing every connection,
    /// which is how one unreachable GNU mirror took out three toolchain
    /// builds. The checksum is what makes several sources safe to try.
    ///
    /// A mirror serving different bytes is not a network problem and does
    /// not fall through to the next source: pinned inputs are the point, so
    /// a mismatch is surfaced rather than papered over.
    pub fn fetch_cached_any(&self, urls: &[String], sha256: &str) -> Result<PathBuf> {
        let mut last = None;
        for (index, url) in urls.iter().enumerate() {
            match self.fetch_cached(url, sha256) {
                Ok(path) => return Ok(path),
                Err(e @ Error::ChecksumMismatch { .. }) => return Err(e),
                Err(e) => {
                    if index + 1 < urls.len() {
                        tracing::warn!(url, error = %e, "source failed; trying the next mirror");
                    }
                    last = Some(e);
                }
            }
        }
        Err(last.unwrap_or_else(|| Error::Http {
            url: String::new(),
            reason: "no source URLs configured".to_string(),
        }))
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

    #[test]
    fn only_overload_and_server_faults_are_retried() {
        assert!(is_retryable_status(429));
        assert!(is_retryable_status(500));
        assert!(is_retryable_status(503));
        // A wrong URL or a missing file stays wrong.
        assert!(!is_retryable_status(404));
        assert!(!is_retryable_status(403));
        assert!(!is_retryable_status(200));
    }
}
