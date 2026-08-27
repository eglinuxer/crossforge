//! Checked primitives shared by the binary parsers (ELF, RPM, ar, zip).
//!
//! Every one of those formats reaches crossforge as bytes it did not
//! produce: wheels from an index, RPMs from a mirror, binaries a caller
//! hands to the audit gate. On hostile or merely truncated input the offset
//! arithmetic overflows well before it goes out of bounds — `shoff + i *
//! shentsize` with a garbage `shoff` panics in debug and silently wraps in
//! release. A gate that crashes is worse than one that reports, and as a
//! library a panic takes the caller's process down, so the parsers compute
//! offsets here.
//!
//! These return `Option` rather than `Result`: each parser keeps its own
//! error variant and message.

/// Checked `base + delta`.
pub(crate) fn add(base: usize, delta: usize) -> Option<usize> {
    base.checked_add(delta)
}

/// Checked `base + index * size`, the shape every table walk needs.
pub(crate) fn span(base: usize, index: usize, size: usize) -> Option<usize> {
    index.checked_mul(size).and_then(|o| base.checked_add(o))
}

/// Bounds-checked subslice; `at + len` cannot overflow into a valid range.
pub(crate) fn slice(data: &[u8], at: usize, len: usize) -> Option<&[u8]> {
    let end = at.checked_add(len)?;
    data.get(at..end)
}

pub(crate) fn u16le(data: &[u8], at: usize) -> Option<u16> {
    Some(u16::from_le_bytes(slice(data, at, 2)?.try_into().ok()?))
}

pub(crate) fn u32le(data: &[u8], at: usize) -> Option<u32> {
    Some(u32::from_le_bytes(slice(data, at, 4)?.try_into().ok()?))
}

pub(crate) fn u64le(data: &[u8], at: usize) -> Option<u64> {
    Some(u64::from_le_bytes(slice(data, at, 8)?.try_into().ok()?))
}

/// NUL-terminated string starting at `at`, as far as the slice allows.
pub(crate) fn cstr(data: &[u8], at: usize) -> Option<&[u8]> {
    let rest = data.get(at..)?;
    let end = rest.iter().position(|c| *c == 0)?;
    Some(&rest[..end])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arithmetic_saturates_into_none_instead_of_panicking() {
        assert_eq!(add(usize::MAX, 1), None);
        assert_eq!(span(0, usize::MAX, 64), None);
        assert_eq!(span(usize::MAX - 1, 1, 64), None);
        assert_eq!(span(8, 2, 4), Some(16));
    }

    #[test]
    fn reads_stay_inside_the_slice() {
        let data = [1u8, 2, 3, 4, 5, 6, 7, 8];
        assert_eq!(u16le(&data, 0), Some(0x0201));
        assert_eq!(u32le(&data, 5), None, "would run past the end");
        assert_eq!(u64le(&data, 0).is_some(), true);
        assert_eq!(slice(&data, usize::MAX, 4), None);
        assert_eq!(cstr(&data, 0), None, "no terminator");
        assert_eq!(cstr(b"ab\0c", 0), Some(&b"ab"[..]));
        assert_eq!(cstr(b"ab\0c", 9), None);
    }
}
