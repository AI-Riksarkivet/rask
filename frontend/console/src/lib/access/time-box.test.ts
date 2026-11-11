import { formatZoned } from '@rask/ui/utils';
import { describe, expect, it } from 'vitest';
import { durationMs, timeBoxLabel } from './time-box.ts';

const MODEL = {
	non_expired_grant: {
		current_time: 'timestamp',
		grant_time: 'timestamp',
		grant_duration: 'duration',
	},
};

describe('durationMs', () => {
	it.each([
		['1h', 3_600_000],
		['1h30m', 5_400_000],
		['1.5h', 5_400_000],
	])('reads %s as %i ms', (text, ms) => {
		expect(durationMs(text)).toBe(ms);
	});

	it('refuses text that is not a whole duration', () => {
		expect(durationMs('2026-11-11T19:30:26Z')).toBeNull();
		expect(durationMs('1 hour')).toBeNull();
	});
});

describe('timeBoxLabel', () => {
	it('names the duration and the moment the window closes, by the parameters the model types', () => {
		const start = '2026-11-11T19:30:26.134Z';
		expect(
			timeBoxLabel(
				{ name: 'non_expired_grant', context: { grant_time: start, grant_duration: '1h' } },
				MODEL,
			),
		).toBe(`for 1h, until ${formatZoned(Date.parse(start) + 3_600_000)}`);
	});

	it('is null for a permanent grant and for a condition the model gives no duration', () => {
		expect(timeBoxLabel(null, MODEL)).toBeNull();
		expect(timeBoxLabel({ name: 'other', context: { region: 'eu' } }, MODEL)).toBeNull();
	});
});
