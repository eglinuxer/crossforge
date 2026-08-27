//! Minimal GNU `ar` archive reader, for slicing static libraries into member
//! objects (compat-pack generation).

use crate::bytes;
use crate::error::{Error, Result};

fn ar_err(msg: impl Into<String>) -> Error {
    Error::Archive(msg.into())
}

/// One archive member: its file name and raw contents.
#[derive(Debug, Clone)]
pub struct Member<'a> {
    pub name: String,
    pub data: &'a [u8],
}

/// Parses a GNU ar archive, resolving long names and skipping the symbol
/// index (`/`) and long-name table (`//`) pseudo-members.
pub fn parse(data: &[u8]) -> Result<Vec<Member<'_>>> {
    if data.get(..8) != Some(b"!<arch>\n".as_slice()) {
        return Err(ar_err("bad archive magic"));
    }
    let mut pos = 8usize;
    let mut longnames: &[u8] = &[];
    let mut members = Vec::new();
    while pos + 60 <= data.len() {
        let header =
            bytes::slice(data, pos, 60).ok_or_else(|| Error::Archive("truncated header".into()))?;
        if &header[58..60] != b"`\n" {
            return Err(ar_err("bad member header terminator"));
        }
        let name_field = std::str::from_utf8(&header[..16])
            .map_err(|_| ar_err("non-ascii member name"))?
            .trim_end();
        let size: usize = std::str::from_utf8(&header[48..58])
            .map_err(|_| ar_err("bad size field"))?
            .trim_end()
            .parse()
            .map_err(|_| ar_err("bad size field"))?;
        let body = data
            .get(pos + 60..pos + 60 + size)
            .ok_or_else(|| ar_err("truncated member"))?;
        pos += 60 + size + size % 2;

        match name_field {
            "/" | "/SYM64/" => {} // symbol index
            "//" => longnames = body,
            _ => {
                let name = if let Some(offset) = name_field.strip_prefix('/') {
                    let offset: usize =
                        offset.parse().map_err(|_| ar_err("bad long-name offset"))?;
                    let rest = longnames
                        .get(offset..)
                        .ok_or_else(|| ar_err("long-name offset outside table"))?;
                    let end = rest
                        .iter()
                        .position(|c| *c == b'/' || *c == b'\n')
                        .unwrap_or(rest.len());
                    String::from_utf8(rest[..end].to_vec())
                        .map_err(|_| ar_err("non-utf8 long name"))?
                } else {
                    name_field.trim_end_matches('/').to_string()
                };
                members.push(Member { name, data: body });
            }
        }
    }
    Ok(members)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(name_field: &str, data: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(format!("{name_field:<16}").as_bytes());
        out.extend_from_slice(format!("{:<12}", 0).as_bytes());
        out.extend_from_slice(format!("{:<6}", 0).as_bytes());
        out.extend_from_slice(format!("{:<6}", 0).as_bytes());
        out.extend_from_slice(format!("{:<8}", "644").as_bytes());
        out.extend_from_slice(format!("{:<10}", data.len()).as_bytes());
        out.extend_from_slice(b"`\n");
        out.extend_from_slice(data);
        if data.len() % 2 == 1 {
            out.push(b'\n');
        }
        out
    }

    #[test]
    fn parses_short_and_long_names() {
        let mut ar = b"!<arch>\n".to_vec();
        ar.extend_from_slice(&entry("/", b"\x00\x00\x00\x00")); // symbol index
        ar.extend_from_slice(&entry("//", b"very-long-member-name.o/\n"));
        ar.extend_from_slice(&entry("short.o/", b"AAA"));
        ar.extend_from_slice(&entry("/0", b"BBBB"));
        let members = parse(&ar).unwrap();
        assert_eq!(members.len(), 2);
        assert_eq!(members[0].name, "short.o");
        assert_eq!(members[0].data, b"AAA");
        assert_eq!(members[1].name, "very-long-member-name.o");
        assert_eq!(members[1].data, b"BBBB");
    }

    #[test]
    fn rejects_bad_magic() {
        assert!(matches!(parse(b"not an archive"), Err(Error::Archive(_))));
    }
}
