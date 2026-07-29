import { describe, it, expect } from 'vitest';
import { cn } from '../src/lib/utils/cn.js';

describe('cn', () => {
	it('merges tailwind classes, last one wins on conflict', () => {
		expect(cn('p-2', 'p-4')).toBe('p-4');
	});

	it('handles conditional values', () => {
		// The constant `false &&` is the POINT: this pins the call-site idiom every component uses
		// (`cn(base, isActive && 'active')`) rather than just cn's handling of a bare falsy argument.
		// oxlint-disable-next-line no-constant-binary-expression
		expect(cn('text-sm', false && 'hidden', 'font-bold')).toBe('text-sm font-bold');
	});
});
