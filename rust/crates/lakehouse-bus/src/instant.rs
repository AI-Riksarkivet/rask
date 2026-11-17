//! The instant an envelope's `time` attribute names: read from an OpenLineage `eventTime` as Python's
//! `datetime.fromisoformat` reads it, read from a staged envelope as pydantic reads an aware datetime, and written as
//! pydantic writes one. An instant with no offset is no point in time, and neither reader yields one. Both read the
//! extended calendar date only, the form RFC 3339 and every lakehouse writer use (ADR 0225).

use serde_json::Value;

/// Hours, minutes, seconds and microseconds.
type Clock = (u32, u32, u32, u32);
type Date = (u32, u32, u32);

const MICROS: i64 = 1_000_000;
/// The furthest an offset may lie from UTC, exclusive: a day.
const DAY_MICROS: i64 = 86_400 * MICROS;

/// A wall-clock time with its offset from UTC.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Instant {
    date: Date,
    clock: Clock,
    offset_micros: i64,
}

impl Instant {
    fn new(date: Date, clock: Clock, offset_micros: i64) -> Option<Self> {
        let ((year, month, day), (hour, minute, second, _)) = (date, clock);
        let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
        let month_days = match month {
            2 => 28 + u32::from(leap),
            4 | 6 | 9 | 11 => 30,
            _ => 31,
        };
        let valid = (1..=9999).contains(&year) && (1..=12).contains(&month) && (1..=month_days).contains(&day);
        let valid = valid && hour < 24 && minute < 60 && second < 60 && offset_micros.abs() < DAY_MICROS;
        valid.then_some(Instant { date, clock, offset_micros })
    }

    /// The instant as pydantic writes it in JSON: seconds always, microseconds where there are any, and the offset in
    /// whole minutes (`Z` for UTC).
    pub fn render(&self) -> String {
        let ((year, month, day), (hour, minute, second, micro)) = (self.date, self.clock);
        let mut text = format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}");
        if micro > 0 {
            text.push_str(&format!(".{micro:06}"));
        }
        let half = if self.offset_micros < 0 { -MICROS / 2 } else { MICROS / 2 };
        let seconds = (self.offset_micros + half) / MICROS;
        let minutes = seconds.abs() / 60;
        match seconds {
            0 => text.push('Z'),
            _ => text.push_str(&format!("{}{:02}:{:02}", if seconds < 0 { '-' } else { '+' }, minutes / 60, minutes % 60)),
        }
        text
    }
}

/// `count` ASCII digits of `text` from `at`, as a number.
fn digits(text: &[char], at: usize, count: usize) -> Option<u32> {
    let slice = text.get(at..at + count)?;
    slice
        .iter()
        .try_fold(0u32, |sum, c| Some(sum * 10 + c.to_digit(10).filter(|_| c.is_ascii_digit())?))
}

/// `YYYY-MM-DD` at the start of `text`.
fn date(text: &[char]) -> Option<Date> {
    (text.get(4) == Some(&'-') && text.get(7) == Some(&'-')).then_some(())?;
    Some((digits(text, 0, 4)?, digits(text, 5, 2)?, digits(text, 8, 2)?))
}

/// The instant an OpenLineage `eventTime` names, as Python's `datetime.fromisoformat` reads it: the date, any one
/// character, and the time with its offset. None for text it reads as no instant or as a time with no offset.
pub fn from_isoformat(text: &str) -> Option<Instant> {
    let chars: Vec<char> = text.chars().collect();
    let (clock, offset) = iso_time(chars.get(11..).filter(|time| !time.is_empty())?)?;
    Instant::new(date(&chars)?, clock, offset?)
}

/// What CPython's `parse_hh_mm_ss_ff` makes of `text` (`HH[:?MM[:?SS[{.,}f…]]]`), and whether anything is left after it.
fn hh_mm_ss_ff(text: &[char]) -> Option<(Clock, bool)> {
    let mut values = [0u32; 3];
    let (mut at, mut separated, mut fraction) = (0, true, false);
    for (index, value) in values.iter_mut().enumerate() {
        *value = digits(text, at, 2)?;
        let next = text.get(at + 2).copied();
        at += 3;
        if index == 0 {
            separated = next == Some(':');
        }
        if at >= text.len() {
            return Some(((values[0], values[1], values[2], 0), at == text.len() && next.is_some()));
        }
        match next {
            Some(':') if separated => {}
            Some('.' | ',') => {
                fraction = true;
                break;
            }
            _ if !separated => at -= 1,
            _ => return None,
        }
    }
    let mut micro = 0;
    if fraction {
        let count = (text.len() - at).min(6);
        micro = digits(text, at, count).filter(|_| count > 0)? * 10u32.pow(u32::try_from(6 - count).unwrap_or(0));
        at += count;
        while text.get(at).is_some_and(char::is_ascii_digit) {
            at += 1;
        }
    }
    Some(((values[0], values[1], values[2], micro), at < text.len()))
}

/// The time of an ISO string and its offset in microseconds (None for a time with none), as CPython's
/// `parse_isoformat_time` reads it.
fn iso_time(text: &[char]) -> Option<(Clock, Option<i64>)> {
    let zone = text.iter().position(|c| matches!(c, 'Z' | '+' | '-')).unwrap_or(text.len());
    let (clock, left) = hh_mm_ss_ff(&text[..zone])?;
    match text.get(zone) {
        None => (!left).then_some((clock, None)),
        Some('Z') => (zone + 1 == text.len()).then_some((clock, Some(0))),
        Some(sign) => {
            let ((hours, minutes, seconds, micros), left) = hh_mm_ss_ff(&text[zone + 1..])?;
            let offset = (i64::from(hours) * 3600 + i64::from(minutes) * 60 + i64::from(seconds)) * MICROS + i64::from(micros);
            (!left).then_some((clock, Some(if *sign == '-' { -offset } else { offset })))
        }
    }
}

/// The instant a staged envelope's `time` holds, as pydantic reads an aware datetime from JSON text: `YYYY-MM-DD`, a
/// separator (`T`, `t`, `_` or a space), `HH:MM[:SS[{.,}f…]]` and an offset (`Z`, `z`, `±HH:MM` or `±HHMM`). None for
/// a value pydantic refuses, and for a unix timestamp, which no writer stages (ADR 0225).
pub fn parsed(value: &Value) -> Option<Instant> {
    let chars: Vec<char> = value.as_str()?.chars().collect();
    let at = |index: usize| chars.get(index).copied();
    if !matches!(at(10), Some('T' | 't' | '_' | ' ')) || at(13) != Some(':') {
        return None;
    }
    let (hour, minute) = (digits(&chars, 11, 2)?, digits(&chars, 14, 2)?);
    let (mut index, mut second, mut micro) = (16, 0, 0);
    if at(index) == Some(':') {
        second = digits(&chars, index + 1, 2)?;
        index += 3;
        if matches!(at(index), Some('.' | ',')) {
            let start = index + 1;
            index = start + chars[start..].iter().take_while(|c| c.is_ascii_digit()).count();
            let kept = (index - start).min(6);
            micro = digits(&chars, start, kept).filter(|_| kept > 0)? * 10u32.pow(u32::try_from(6 - kept).unwrap_or(0));
        }
    }
    let offset = match &chars[index.min(chars.len())..] {
        ['Z' | 'z'] => 0,
        [sign @ ('+' | '-'), rest @ ..] if matches!(rest, [_, _, ':', _, _] | [_, _, _, _]) => {
            let (hours, minutes) = (
                digits(rest, 0, 2).filter(|hours| *hours < 24)?,
                digits(rest, rest.len() - 2, 2).filter(|m| *m < 60)?,
            );
            let magnitude = (i64::from(hours) * 3600 + i64::from(minutes) * 60) * MICROS;
            if *sign == '-' { -magnitude } else { magnitude }
        }
        _ => return None,
    };
    Instant::new(date(&chars)?, (hour, minute, second, micro), offset)
}
