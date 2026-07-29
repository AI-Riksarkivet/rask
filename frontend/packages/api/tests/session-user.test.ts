/**
 * Unit tests for `sessionToUser` — the single-sourced projection every zone's +layout.server.ts uses to
 * feed the shared AppShell's nav-user. Pinning it here means "auth is identical in every MFE" is enforced
 * by ONE tested function, not five hand-copied layout loaders.
 */
import { describe, expect, test } from 'vitest';
import type { Session } from '../src/oidc';
import { sessionToUser } from '../src/bff';

const session = (over: Partial<Session> = {}): Session => ({
	sub: 'user:alice',
	name: 'Alice Ackerman',
	email: 'alice@example.com',
	accessToken: 'tok',
	expiresAt: 0,
	...over,
});

describe('sessionToUser', () => {
	test('returns null when signed out', () => {
		expect(sessionToUser(null)).toBeNull();
	});

	test('projects name + email and derives two-letter initials from the first two words', () => {
		expect(sessionToUser(session())).toEqual({
			name: 'Alice Ackerman',
			email: 'alice@example.com',
			initials: 'AA',
		});
	});

	test('a single-word name yields its first two letters, uppercased', () => {
		expect(sessionToUser(session({ name: 'root' }))?.initials).toBe('RO');
	});

	test('falls back to email then sub when the name is empty, and omits an absent email', () => {
		const noName = sessionToUser(session({ name: '', email: 'bob@corp.io' }));
		expect(noName?.name).toBe('bob@corp.io');
		expect(noName?.initials).toBe('BO');

		// exactOptionalPropertyTypes: a null email is OMITTED (never an explicit undefined key).
		const noEmail = sessionToUser(session({ name: 'Carol', email: null }));
		expect(noEmail).toEqual({ name: 'Carol', initials: 'CA' });
		expect('email' in noEmail!).toBe(false);

		const bare = sessionToUser(session({ name: '', email: null }));
		expect(bare?.name).toBe('user:alice');
	});
});
