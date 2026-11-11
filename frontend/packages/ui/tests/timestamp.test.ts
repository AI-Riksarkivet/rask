import { describe, expect, it } from 'vitest';
import { formatAbsolute, formatZoned, toDate, utcOffsetLabel } from '../src/lib/utils/timestamp.js';

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

describe('utcOffsetLabel', () => {
	it.each([
		[0, 'UTC'],
		[60, 'UTC+01:00'],
		[-330, 'UTC-05:30'],
	])('names %i minutes east of Greenwich as %s', (minutes, label) => {
		expect(utcOffsetLabel(minutes)).toBe(label);
	});
});

describe('formatZoned', () => {
	it('prints the local stamp followed by the zone it is read in', () => {
		const zone = utcOffsetLabel(-new Date(MS).getTimezoneOffset());
		expect(formatZoned(NANOS)).toBe(`${formatAbsolute(NANOS)} ${zone}`);
	});

	it('renders an em dash when there is nothing to show', () => {
		expect(formatZoned(null)).toBe('—');
	});
});
