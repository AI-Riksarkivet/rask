//! What a stream holds on a subject after a place in it: the read by sequence the catalog's events poll makes.

use lakehouse_core::delivery::Delivery;

/// What a stream holds on a subject after a place in it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamPage {
    /// The messages after the place, in the stream's order, at most the page's limit.
    pub messages: Vec<Delivery>,
    /// The sequence of the last message the stream holds on the subject; 0 for one that holds none.
    pub end: u64,
    /// Whether a message after the place may be gone: the stream's first message after it is not the next sequence, it
    /// holds none after it though its end is past it, or the place is past its end (a stream made again).
    pub gap: bool,
}

impl StreamPage {
    /// Where a read after `after` begins on a subject whose last sequence is `end`: at `after`, at the last `limit`
    /// messages for a new reader (`after` 0), and nowhere for a place past the end.
    pub fn first_place(after: u64, end: u64, limit: u64) -> Option<u64> {
        if after > end {
            return None;
        }
        Some(if after > 0 { after } else { end.saturating_sub(limit) })
    }

    /// The page a read after `after` found: `messages`, read on a subject whose last sequence is `end`.
    pub fn of(after: u64, end: u64, messages: Vec<Delivery>) -> Self {
        let first = messages.first().map(|message| message.sequence);
        let gap = after > end
            || (after > 0
                && match first {
                    None => end > after,
                    Some(first) => first > after + 1,
                });
        StreamPage { messages, end, gap }
    }
}
