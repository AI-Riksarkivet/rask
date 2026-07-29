/**
 * One shared way to render a moment in time across the estate.
 *
 * The panels are fed by three different clocks — the catalog's ISO strings, JS `Date`s, and
 * GreptimeDB's raw NANOSECOND epoch integers (the audit trail's `timestamp` column arrives as
 * `1753387234123456789`, which `new Date(...)` cannot parse at all) — so every surface used to
 * invent its own `when()` and the ones fed nanoseconds simply printed the integer. This module is
 * the single parser + formatter: it takes any of those, decides the epoch UNIT by magnitude, and
 * gives back both an absolute stamp and a compact relative one.
 */

/** Anything a panel might hold for a moment in time. */
export type TimestampInput = string | number | Date | null | undefined;

/** A formatted moment: the exact stamp, the human distance, and the string to hang in a tooltip. */
export type FormattedTimestamp = {
	/** `YYYY-MM-DD HH:mm:ss` in the viewer's local zone. */
	absolute: string;
	/** Compact distance from `now` — `5m ago`, `in 2h`, `just now`. */
	relative: string;
	/** What to put in `title=`: the absolute stamp (so the relative text stays hoverable). */
	title: string;
};

/** Rendered in place of any of the three when the value is absent or unparseable. */
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

/** `YYYY-MM-DD HH:mm:ss` in the viewer's LOCAL zone (so it reads as the operator's wall clock), or
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

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const MONTH = 30 * DAY;
const YEAR = 365 * DAY;

/** Compact distance from `now`: `just now`, `5m ago`, `in 2h`, `3d ago`. Past and future both, so a
 *  clock-skewed or scheduled timestamp reads honestly instead of as a negative age. */
export function formatRelative(value: TimestampInput, now: number = Date.now()): string {
	const date = toDate(value);
	if (!date) return UNKNOWN;
	const delta = now - date.getTime();
	const ago = delta >= 0;
	const abs = Math.abs(delta);
	if (abs < 10_000) return 'just now';

	let amount: string;
	if (abs < MINUTE) amount = `${Math.floor(abs / 1000)}s`;
	else if (abs < HOUR) amount = `${Math.floor(abs / MINUTE)}m`;
	else if (abs < DAY) amount = `${Math.floor(abs / HOUR)}h`;
	else if (abs < MONTH) amount = `${Math.floor(abs / DAY)}d`;
	else if (abs < YEAR) amount = `${Math.floor(abs / MONTH)}mo`;
	else amount = `${Math.floor(abs / YEAR)}y`;
	return ago ? `${amount} ago` : `in ${amount}`;
}

/**
 * Both forms at once — the shape a cell wants: show `relative`, hang `title` off it so the exact
 * moment is one hover away. `now` is injectable so a test can pin the clock.
 */
export function formatTimestamp(
	value: TimestampInput,
	now: number = Date.now(),
): FormattedTimestamp {
	const absolute = formatAbsolute(value);
	return { absolute, relative: formatRelative(value, now), title: absolute };
}
