import { describe, expect, it } from 'vitest';
import { activeTenant } from './active-project';

describe('activeTenant', () => {
	it('takes the project from a host-scoped host, as the shell does', () => {
		expect(activeTenant('acme.localhost:5177', '')).toBe('acme');
	});

	it('lets the host win over a stale cookie — the shell resolves it that way round', () => {
		expect(activeTenant('acme.rask.example', 'zulu')).toBe('acme');
	});

	it('falls back to the active-project cookie on a bare host', () => {
		expect(activeTenant('localhost:5177', 'acme')).toBe('acme');
	});

	it('does not read an IPv4 host as a project', () => {
		expect(activeTenant('127.0.0.1:5177', 'acme')).toBe('acme');
	});

	it('answers EMPTY when nothing has been entered — never a guessed tenant', () => {
		expect(activeTenant('localhost:5177', '')).toBe('');
	});
});
