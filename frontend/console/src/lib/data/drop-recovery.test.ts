import { describe, expect, it } from 'vitest';
import { dropRecovery } from './drop-recovery';

const NOW = Date.UTC(2026, 10, 11, 19, 0, 0);

describe('dropRecovery', () => {
	it('keeps a soft drop recoverable for the warehouse grace period', () => {
		expect(dropRecovery({ mode: 'soft', grace_days: 1 / 24 }, NOW)).toEqual({
			kind: 'recoverable',
			until: NOW + 3_600_000,
		});
	});

	it.each([
		[{ mode: 'hard' as const }, 'permanent'],
		[{ mode: 'estate' as const }, 'estate-default'],
		[{ mode: 'soft' as const, grace_days: 0 }, 'estate-default'],
		[{ mode: 'soft' as const, grace_days: null }, 'estate-default'],
		[null, 'estate-default'],
		[undefined, 'unknown'],
	])('reads %o as %s', (profile, kind) => {
		expect(dropRecovery(profile, NOW).kind).toBe(kind);
	});
});
