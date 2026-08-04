// #68 — the subject shortener the access surfaces share. The observed failure it exists for:
// two holders on one row rendered as "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fsroot_admin"
// — one dex sub glued to `root_admin`, unreadable and unrecognizable as two subjects.
import { describe, expect, it } from 'vitest';
import { subjectDisplay } from './subject.js';

const DEX_SUB = 'CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs';

describe('subjectDisplay', () => {
	it('ellipsizes an opaque OIDC sub in the middle and keeps the full value as title', () => {
		const d = subjectDisplay(DEX_SUB);
		expect(d.label).not.toBe(DEX_SUB);
		expect(d.label).toContain('…');
		expect(d.label.length).toBeLessThan(25);
		expect(d.title).toBe(DEX_SUB);
	});

	it('keeps the type prefix on a typed opaque subject', () => {
		const d = subjectDisplay(`user:${DEX_SUB}`);
		expect(d.label.startsWith('user:')).toBe(true);
		expect(d.label).toContain('…');
		expect(d.title).toBe(`user:${DEX_SUB}`);
	});

	it.each(['root_admin', 'alice@example.com', 'role:steward#member', 'team:htr#member', '*'])(
		'passes the readable subject %s through verbatim',
		(s) => {
			expect(subjectDisplay(s)).toEqual({ label: s, title: s });
		},
	);

	it('does not shorten a short token that merely looks base64ish', () => {
		expect(subjectDisplay('abc123').label).toBe('abc123');
	});
});
