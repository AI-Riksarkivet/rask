//! How a page of the feed is read when only some of its rows are visible to the caller.

/// The most reads one page makes of the feed.
pub const MOST_READS: u64 = 3;
const CHUNK_GROWTH: u64 = 8;
const CHUNK_MARGIN: u64 = 4;

/// How many rows the next read takes, after `reads` reads took `read` rows of which `visible` were shown.
///
/// The last read takes everything `left`. Before it, a page that showed nothing grows the read, and one that showed
/// some reads what its hit rate says the rest of the page needs, with a margin.
pub fn next_chunk(limit: u64, read: u64, visible: u64, left: u64, reads: u64) -> u64 {
    if reads + 1 >= MOST_READS {
        return left;
    }
    if visible == 0 {
        return left.min(read * CHUNK_GROWTH);
    }
    left.min((limit.saturating_sub(visible) * read).div_ceil(visible) * CHUNK_MARGIN)
}

/// Where a caller following the feed forward reads from next.
///
/// A full page continues after its last row. A short one continues after the last row read, shown or not, and an
/// empty read stays where it was, never ahead of the feed's `head`.
pub fn forward_cursor(last_returned: Option<u64>, returned: usize, last_read: Option<u64>, limit: usize, since: u64, head: u64) -> u64 {
    match (last_returned, last_read) {
        (Some(seq), _) if returned == limit => seq,
        (_, Some(seq)) => seq,
        _ => since.min(head),
    }
}
