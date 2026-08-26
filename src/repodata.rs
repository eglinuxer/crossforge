//! Yum/dnf repository metadata: `repomd.xml` and `primary.xml` parsing, plus
//! RPM version comparison for picking the newest build of each package.

use std::cmp::Ordering;

use quick_xml::Reader;
use quick_xml::events::Event;

use crate::error::{Error, Result};

/// One package entry from `primary.xml`.
#[derive(Debug, Clone, Default)]
pub struct RepoPackage {
    pub name: String,
    pub arch: String,
    pub epoch: String,
    pub version: String,
    pub release: String,
    /// Path relative to the repo root, e.g. `Packages/glibc-2.28-251.el8_10.x86_64.rpm`.
    pub location: String,
    /// sha256 of the RPM file.
    pub checksum: String,
}

impl RepoPackage {
    /// `epoch:version-release` display form.
    pub fn evr(&self) -> String {
        if self.epoch.is_empty() || self.epoch == "0" {
            format!("{}-{}", self.version, self.release)
        } else {
            format!("{}:{}-{}", self.epoch, self.version, self.release)
        }
    }

    pub fn evr_cmp(&self, other: &Self) -> Ordering {
        let ea = if self.epoch.is_empty() {
            "0"
        } else {
            &self.epoch
        };
        let eb = if other.epoch.is_empty() {
            "0"
        } else {
            &other.epoch
        };
        rpmvercmp(ea, eb)
            .then_with(|| rpmvercmp(&self.version, &other.version))
            .then_with(|| rpmvercmp(&self.release, &other.release))
    }
}

fn xml_err(e: impl std::fmt::Display) -> Error {
    Error::RepoMetadata(e.to_string())
}

/// Parses `repomd.xml` and returns `(location, sha256)` of the primary metadata.
pub fn parse_repomd(xml: &[u8]) -> Result<(String, String)> {
    let mut reader = Reader::from_reader(xml);
    let mut buf = Vec::new();
    let mut in_primary = false;
    let mut in_checksum = false;
    let mut location = None;
    let mut checksum = None;
    loop {
        match reader.read_event_into(&mut buf).map_err(xml_err)? {
            Event::Start(e) | Event::Empty(e) => {
                let name = e.local_name();
                match name.as_ref() {
                    b"data" => {
                        in_primary = e.attributes().flatten().any(|a| {
                            a.key.local_name().as_ref() == b"type" && a.value.as_ref() == b"primary"
                        });
                    }
                    b"location" if in_primary => {
                        for a in e.attributes().flatten() {
                            if a.key.local_name().as_ref() == b"href" {
                                location = Some(String::from_utf8_lossy(&a.value).into_owned());
                            }
                        }
                    }
                    b"checksum" if in_primary => in_checksum = true,
                    _ => {}
                }
            }
            Event::Text(t) if in_primary && in_checksum => {
                checksum = Some(String::from_utf8_lossy(t.as_ref()).trim().to_string());
            }
            Event::End(e) => match e.local_name().as_ref() {
                b"checksum" => in_checksum = false,
                b"data" => in_primary = false,
                _ => {}
            },
            Event::Eof => break,
            _ => {}
        }
        buf.clear();
    }
    match (location, checksum) {
        (Some(l), Some(c)) => Ok((l, c)),
        _ => Err(Error::RepoMetadata(
            "repomd.xml has no primary data entry".to_string(),
        )),
    }
}

/// Parses an uncompressed `primary.xml` into package entries.
pub fn parse_primary(xml: &[u8]) -> Result<Vec<RepoPackage>> {
    let mut reader = Reader::from_reader(xml);
    let mut buf = Vec::new();
    let mut packages = Vec::new();
    let mut current: Option<RepoPackage> = None;
    // Text-bearing element we are inside of (name/arch/checksum), package level only.
    let mut text_field: Option<&'static str> = None;
    // <format> subtree contains rpm:* entries we must not confuse with package fields.
    let mut in_format = false;
    loop {
        match reader.read_event_into(&mut buf).map_err(xml_err)? {
            Event::Start(e) | Event::Empty(e) => {
                let local = e.local_name();
                match local.as_ref() {
                    b"package" => current = Some(RepoPackage::default()),
                    b"format" => in_format = true,
                    b"name" if !in_format => text_field = Some("name"),
                    b"arch" if !in_format => text_field = Some("arch"),
                    b"checksum" if !in_format => text_field = Some("checksum"),
                    b"version" if !in_format => {
                        if let Some(pkg) = current.as_mut() {
                            for a in e.attributes().flatten() {
                                let value = String::from_utf8_lossy(&a.value).into_owned();
                                match a.key.local_name().as_ref() {
                                    b"epoch" => pkg.epoch = value,
                                    b"ver" => pkg.version = value,
                                    b"rel" => pkg.release = value,
                                    _ => {}
                                }
                            }
                        }
                    }
                    b"location" if !in_format => {
                        if let Some(pkg) = current.as_mut() {
                            for a in e.attributes().flatten() {
                                if a.key.local_name().as_ref() == b"href" {
                                    pkg.location = String::from_utf8_lossy(&a.value).into_owned();
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
            Event::Text(t) => {
                if let (Some(field), Some(pkg)) = (text_field, current.as_mut()) {
                    let value = String::from_utf8_lossy(t.as_ref()).trim().to_string();
                    match field {
                        "name" => pkg.name = value,
                        "arch" => pkg.arch = value,
                        "checksum" => pkg.checksum = value,
                        _ => {}
                    }
                }
            }
            Event::End(e) => match e.local_name().as_ref() {
                b"package" => {
                    if let Some(pkg) = current.take() {
                        packages.push(pkg);
                    }
                }
                b"format" => in_format = false,
                b"name" | b"arch" | b"checksum" => text_field = None,
                _ => {}
            },
            Event::Eof => break,
            _ => {}
        }
        buf.clear();
    }
    Ok(packages)
}

/// The classic rpmvercmp algorithm: alternating numeric / alphabetic segments,
/// numeric beats alphabetic, `~` sorts before everything.
pub fn rpmvercmp(a: &str, b: &str) -> Ordering {
    let a = a.as_bytes();
    let b = b.as_bytes();
    let (mut i, mut j) = (0, 0);
    loop {
        // Skip separators (anything that is not alphanumeric or tilde).
        while i < a.len() && !a[i].is_ascii_alphanumeric() && a[i] != b'~' {
            i += 1;
        }
        while j < b.len() && !b[j].is_ascii_alphanumeric() && b[j] != b'~' {
            j += 1;
        }
        // Tilde: pre-release marker, sorts before everything including end.
        let ta = i < a.len() && a[i] == b'~';
        let tb = j < b.len() && b[j] == b'~';
        match (ta, tb) {
            (true, true) => {
                i += 1;
                j += 1;
                continue;
            }
            (true, false) => return Ordering::Less,
            (false, true) => return Ordering::Greater,
            (false, false) => {}
        }
        if i >= a.len() || j >= b.len() {
            return (a.len() - i).cmp(&(b.len() - j));
        }
        // Take one segment of the same character class from each side.
        let numeric = a[i].is_ascii_digit();
        let sa = take_segment(a, &mut i, numeric);
        let sb = take_segment(b, &mut j, numeric);
        if sb.is_empty() {
            // Different classes: numeric segment beats alphabetic.
            return if numeric {
                Ordering::Greater
            } else {
                Ordering::Less
            };
        }
        let ord = if numeric {
            // Longer digit string (after stripping leading zeros) is larger;
            // equal lengths compare lexically.
            let sa = strip_leading_zeros(sa);
            let sb = strip_leading_zeros(sb);
            sa.len().cmp(&sb.len()).then_with(|| sa.cmp(sb))
        } else {
            sa.cmp(sb)
        };
        if ord != Ordering::Equal {
            return ord;
        }
    }
}

fn strip_leading_zeros(s: &[u8]) -> &[u8] {
    let n = s.iter().take_while(|c| **c == b'0').count();
    &s[n..]
}

fn take_segment<'a>(s: &'a [u8], pos: &mut usize, numeric: bool) -> &'a [u8] {
    let start = *pos;
    while *pos < s.len() {
        let c = s[*pos];
        let same = if numeric {
            c.is_ascii_digit()
        } else {
            c.is_ascii_alphabetic()
        };
        if !same {
            break;
        }
        *pos += 1;
    }
    &s[start..*pos]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vercmp_basics() {
        assert_eq!(rpmvercmp("1.0", "1.0"), Ordering::Equal);
        assert_eq!(rpmvercmp("1.0", "1.1"), Ordering::Less);
        assert_eq!(rpmvercmp("1.10", "1.9"), Ordering::Greater);
        assert_eq!(rpmvercmp("1.010", "1.10"), Ordering::Equal);
        assert_eq!(rpmvercmp("1.0a", "1.0"), Ordering::Greater);
        assert_eq!(rpmvercmp("1.0", "1.0.1"), Ordering::Less);
        assert_eq!(rpmvercmp("2a", "2.0"), Ordering::Less); // alpha loses to numeric
        assert_eq!(rpmvercmp("1.0~rc1", "1.0"), Ordering::Less);
        assert_eq!(rpmvercmp("1.0~rc1", "1.0~rc2"), Ordering::Less);
        assert_eq!(rpmvercmp("251.el8_10", "225.el8"), Ordering::Greater);
    }

    #[test]
    fn evr_cmp_uses_epoch_first() {
        let mut a = RepoPackage {
            epoch: "1".into(),
            version: "1.0".into(),
            release: "1".into(),
            ..Default::default()
        };
        let b = RepoPackage {
            epoch: "0".into(),
            version: "9.9".into(),
            release: "9".into(),
            ..Default::default()
        };
        assert_eq!(a.evr_cmp(&b), Ordering::Greater);
        a.epoch = "0".into();
        assert_eq!(a.evr_cmp(&b), Ordering::Less);
    }

    #[test]
    fn parse_repomd_finds_primary() {
        let xml = br#"<?xml version="1.0"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="filelists"><location href="repodata/x-filelists.xml.gz"/><checksum type="sha256">aaa</checksum></data>
  <data type="primary">
    <checksum type="sha256">deadbeef</checksum>
    <open-checksum type="sha256">cafe</open-checksum>
    <location href="repodata/x-primary.xml.gz"/>
  </data>
</repomd>"#;
        let (loc, sum) = parse_repomd(xml).unwrap();
        assert_eq!(loc, "repodata/x-primary.xml.gz");
        assert_eq!(sum, "deadbeef");
    }

    #[test]
    fn parse_primary_extracts_packages() {
        let xml = br#"<?xml version="1.0"?>
<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm">
<package type="rpm">
  <name>glibc</name>
  <arch>x86_64</arch>
  <version epoch="0" ver="2.28" rel="251.el8_10"/>
  <checksum type="sha256" pkgid="YES">abc123</checksum>
  <location href="Packages/glibc-2.28-251.el8_10.x86_64.rpm"/>
  <format>
    <rpm:sourcerpm>glibc-2.28-251.el8_10.src.rpm</rpm:sourcerpm>
    <rpm:provides><rpm:entry name="glibc"/></rpm:provides>
  </format>
</package>
<package type="rpm">
  <name>glibc</name>
  <arch>i686</arch>
  <version epoch="0" ver="2.28" rel="251.el8_10"/>
  <checksum type="sha256" pkgid="YES">def456</checksum>
  <location href="Packages/glibc-2.28-251.el8_10.i686.rpm"/>
</package>
</metadata>"#;
        let pkgs = parse_primary(xml).unwrap();
        assert_eq!(pkgs.len(), 2);
        assert_eq!(pkgs[0].name, "glibc");
        assert_eq!(pkgs[0].arch, "x86_64");
        assert_eq!(pkgs[0].version, "2.28");
        assert_eq!(pkgs[0].release, "251.el8_10");
        assert_eq!(pkgs[0].checksum, "abc123");
        assert_eq!(
            pkgs[0].location,
            "Packages/glibc-2.28-251.el8_10.x86_64.rpm"
        );
        assert_eq!(pkgs[0].evr(), "2.28-251.el8_10");
        assert_eq!(pkgs[1].arch, "i686");
    }
}
