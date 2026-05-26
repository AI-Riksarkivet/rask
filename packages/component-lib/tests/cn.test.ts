import { describe, it, expect } from 'vitest';
import { cn } from '../src/lib/utils/cn.js';

describe('cn', () => {
	it('merges tailwind classes, last one wins on conflict', () => {
		expect(cn('p-2', 'p-4')).toBe('p-4');
	});

	it('handles conditional values', () => {
		expect(cn('text-sm', false && 'hidden', 'font-bold')).toBe('text-sm font-bold');
	});
});
