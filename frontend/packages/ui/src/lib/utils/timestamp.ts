/**
 * One shared way to render a moment in time across the estate.
 *
 * A moment reaches a panel on three different clocks: the catalog's ISO strings, JS `Date`s, and raw
 * NANOSECOND epoch integers (`1753387234123456789`, which `new Date(...)` cannot parse at all). This
 * module is the single parser and formatter: it takes any of those, decides the epoch UNIT by
 * magnitude, and prints the local stamp, with or without the zone it is read in.
 */

/** Anything a panel might hold for a moment in time. */
export type TimestampInput = string | number | Date | null | undefined;

/** Rendered in place of a moment that is absent or unparseable. */
const UNKNOWN = '—';

// Epoch unit by magnitude, in absolute value. A second-epoch stays under 1e11 until the year 5138,
// so these bands separate s / ms / µs / ns without ever needing the caller to declare a unit.
const MAX_SECONDS = 1e11;
const MAX_MILLIS = 1e14;
const MAX_MICROS = 1e17;

/** Milliseconds from an all-digit string, exactly — via BigInt, because a nanosecond epoch has 19
 *  digits and blows past Number.MAX_SAFE_INTEGER (the naive `Number(...)` loses the low digits). */
function millisFromDigits(text: string): number | null {
	let big: bigint;
	try {
		big = BigInt(text);
	} catch {
		return null;
	}
	const magnitude = big < 0n ? -big : big;
	if (magnitude < BigInt(MAX_SECONDS)) return Number(big) * 1000;
	if (magnitude < BigInt(MAX_MILLIS)) return Number(big);
	if (magnitude < BigInt(MAX_MICROS)) return Number(big / 1000n);
	return Number(big / 1_000_000n);
}

/**
 * Parse any accepted input into a `Date`, or `null` when there is nothing to show.
 *
 * Numbers and ALL-DIGIT strings are epochs (unit inferred by magnitude); anything else is handed to
 * `Date.parse`, which covers ISO-8601. A bare numeric string is therefore read as an epoch, never
 * as a year.
 */
export function toDate(value: TimestampInput): Date | null {
	if (value === null || value === undefined || value === '') return null;
	if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;

	let millis: number | null = null;
	if (typeof value === 'number') {
		if (!Number.isFinite(value)) return null;
		millis = millisFromDigits(Math.trunc(value).toString());
	} else {
		const text = value.trim();
		millis = /^-?\d+$/.test(text) ? millisFromDigits(text) : Date.parse(text);
	}
	if (millis === null || Number.isNaN(millis)) return null;
	const date = new Date(millis);
	return Number.isNaN(date.getTime()) ? null : date;
}

const pad = (n: number, width = 2) => String(n).padStart(width, '0');

/** `YYYY-MM-DD HH:mm:ss` in the reader's local time zone (so it reads as their wall clock), or
 *  `—` when there is nothing to format. Local time means SSR and the browser can disagree: format
 *  where the value is rendered, not where it is fetched. */
export function formatAbsolute(value: TimestampInput): string {
	const date = toDate(value);
	if (!date) return UNKNOWN;
	return (
		`${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
		`${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
	);
}

/** The zone a local stamp is read in, as an offset from UTC: `UTC`, `UTC+01:00`, `UTC-05:30`.
 *  `minutesEast` is the offset east of Greenwich, the negation of `Date#getTimezoneOffset()`. An
 *  offset rather than a zone name: the browser's short names differ between engines and locales
 *  (`CET`, `GMT+1`), and an offset reads the same everywhere. */
export function utcOffsetLabel(minutesEast: number): string {
	if (minutesEast === 0) return 'UTC';
	const sign = minutesEast > 0 ? '+' : '-';
	const abs = Math.abs(minutesEast);
	return `UTC${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

/** `YYYY-MM-DD HH:mm:ss UTC+01:00`: the full local date and time with the zone it is read in, the
 *  one form every list of moments in the console prints, so two surfaces never show one instant two
 *  ways. `—` when there is nothing to format. */
export function formatZoned(value: TimestampInput): string {
	const date = toDate(value);
	if (!date) return UNKNOWN;
	return `${formatAbsolute(date)} ${utcOffsetLabel(-date.getTimezoneOffset())}`;
}
