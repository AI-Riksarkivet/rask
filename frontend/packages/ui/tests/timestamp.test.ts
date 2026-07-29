import { describe, expect, it } from 'vitest';
import {
	formatAbsolute,
	formatRelative,
	formatTimestamp,
	toDate,
} from '../src/lib/utils/timestamp.js';

// The estate's one clock formatter. The reason it exists: GreptimeDB hands the audit trail raw
// NANOSECOND epoch integers, which `new Date(...)` cannot parse — so the viewer printed
// "1753387234123456789" at the operator. Every accepted input must land on the same instant.

// 2026-07-24T20:00:34.123Z, in each unit the estate's stores emit.
const MS = Date.UTC(2026, 6, 24, 20, 0, 34, 123);
const SECONDS = Math.floor(MS / 1000);
const MICROS = `${MS}000`;
const NANOS = `${MS}000000`;

describe('toDate', () => {
	it('reads seconds, milliseconds, microseconds and nanoseconds off the magnitude', () => {
		expect(toDate(SECONDS)?.getTime()).toBe(SECONDS * 1000);
		expect(toDate(MS)?.getTime()).toBe(MS);
		expect(toDate(MICROS)?.getTime()).toBe(MS);
		expect(toDate(NANOS)?.getTime()).toBe(MS);
	});

	it('keeps millisecond precision on a nanosecond epoch past MAX_SAFE_INTEGER', () => {
		// 1753387234123456789 > Number.MAX_SAFE_INTEGER: a naive Number() cast rounds the low
		// digits and can shift the millisecond. BigInt division must not.
		expect(Number(NANOS)).toBeGreaterThan(Number.MAX_SAFE_INTEGER);
		expect(toDate(NANOS)?.getMilliseconds()).toBe(new Date(MS).getMilliseconds());
	});

	it('parses ISO strings and passes Dates through', () => {
		expect(toDate('2026-07-24T20:00:34.123Z')?.getTime()).toBe(MS);
		const date = new Date(MS);
		expect(toDate(date)).toBe(date);
	});

	it('returns null for everything unshowable, rather than an Invalid Date', () => {
		for (const bad of [null, undefined, '', '   not a time', Number.NaN, Infinity]) {
			expect(toDate(bad)).toBeNull();
		}
		expect(toDate(new Date('nope'))).toBeNull();
	});
});

describe('formatAbsolute', () => {
	it('renders YYYY-MM-DD HH:mm:ss in the local zone, zero-padded', () => {
		// Compared against the same Date the formatter saw, so the assertion holds in any TZ.
		const local = new Date(MS);
		const expected =
			`${local.getFullYear()}-${String(local.getMonth() + 1).padStart(2, '0')}-` +
			`${String(local.getDate()).padStart(2, '0')} ` +
			`${String(local.getHours()).padStart(2, '0')}:` +
			`${String(local.getMinutes()).padStart(2, '0')}:` +
			`${String(local.getSeconds()).padStart(2, '0')}`;
		expect(formatAbsolute(NANOS)).toBe(expected);
		expect(formatAbsolute(MS)).toBe(expected);
	});

	it('renders an em dash when there is nothing to show', () => {
		expect(formatAbsolute(null)).toBe('—');
		expect(formatAbsolute('garbage')).toBe('—');
	});
});

describe('formatRelative', () => {
	const now = MS;

	it('reports the compact distance into the past', () => {
		expect(formatRelative(MS - 5 * 60_000, now)).toBe('5m ago');
		expect(formatRelative(MS - 30_000, now)).toBe('30s ago');
		expect(formatRelative(MS - 3 * 3_600_000, now)).toBe('3h ago');
		expect(formatRelative(MS - 2 * 86_400_000, now)).toBe('2d ago');
		expect(formatRelative(MS - 45 * 86_400_000, now)).toBe('1mo ago');
		expect(formatRelative(MS - 400 * 86_400_000, now)).toBe('1y ago');
	});

	it('collapses the last few seconds to "just now"', () => {
		expect(formatRelative(MS, now)).toBe('just now');
		expect(formatRelative(MS - 9_000, now)).toBe('just now');
	});

	it('reads a future stamp forwards, never as a negative age', () => {
		expect(formatRelative(MS + 2 * 3_600_000, now)).toBe('in 2h');
		expect(formatRelative(MS + 5 * 60_000, now)).toBe('in 5m');
	});

	it('renders an em dash when there is nothing to show', () => {
		expect(formatRelative(undefined, now)).toBe('—');
	});
});

describe('formatTimestamp', () => {
	it('pairs the relative text with the absolute stamp as its tooltip', () => {
		const t = formatTimestamp(NANOS, MS + 5 * 60_000);
		expect(t.relative).toBe('5m ago');
		expect(t.absolute).toBe(formatAbsolute(NANOS));
		expect(t.title).toBe(t.absolute);
	});
});
