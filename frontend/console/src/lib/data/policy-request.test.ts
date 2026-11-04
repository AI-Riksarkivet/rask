/**
 * One policy form's state → the request body.
 *
 * This existed twice, written identically in `TableDetail.svelte` and the namespace page. When the
 * catalog widened `PolicyRequest` with per-step flags — a policy can now skip a STEP, so
 * `cleanup_enabled` and `optimize_indices_enabled` became required — both copies broke in exactly
 * the same way, and `svelte-check` reported the same error twice.
 *
 * The interesting assertion is the third one. Satisfying the new type by hardcoding the two flags to
 * `true` compiles and is wrong in the dangerous direction: cleanup deletes old versions, so it would
 * run on objects whose owner had explicitly switched maintenance off.
 */

import { describe, expect, it } from 'vitest';

import { policyRequestFrom, type PolicyDraft } from './namespace';

const draft = (extra: Partial<PolicyDraft> = {}): PolicyDraft => ({
	retention_days: null,
	retain_versions: null,
	interval: null,
	target: null,
	enabled: true,
	...extra,
});

describe('policyRequestFrom', () => {
	it('sends every step flag the contract requires', () => {
		const body = policyRequestFrom(draft());

		expect(body.compact_enabled).toBe(true);
		expect(body.cleanup_enabled).toBe(true);
		expect(body.optimize_indices_enabled).toBe(true);
	});

	it('turns EVERY step off when the form is switched off', () => {
		// The one that catches the tempting wrong fix. One switch means "maintenance on this object",
		// not "compaction only" — and the two steps a `true` literal would have pinned on are the
		// destructive ones.
		const body = policyRequestFrom(draft({ enabled: false }));

		expect(body.compact_enabled).toBe(false);
		expect(body.cleanup_enabled).toBe(false);
		expect(body.optimize_indices_enabled).toBe(false);
	});

	it('OMITS an unset knob rather than sending null', () => {
		// Absent means "inherit". Sending null would overwrite an inherited value with an explicit
		// nothing, which is a different and irreversible statement.
		const body = policyRequestFrom(draft());

		expect('retention_days' in body).toBe(false);
		expect('retain_versions' in body).toBe(false);
		expect('compact_interval_hours' in body).toBe(false);
		expect('target_rows_per_fragment' in body).toBe(false);
	});

	it('carries the knobs that ARE set, under their wire names', () => {
		const body = policyRequestFrom(
			draft({ retention_days: 30, retain_versions: 5, interval: 6, target: 1_000_000 }),
		);

		expect(body.retention_days).toBe(30);
		expect(body.retain_versions).toBe(5);
		// The form calls it `interval` and `target`; the wire does not.
		expect(body.compact_interval_hours).toBe(6);
		expect(body.target_rows_per_fragment).toBe(1_000_000);
	});

	it('keeps a zero, which is a value and not an absence', () => {
		const body = policyRequestFrom(draft({ retention_days: 0 }));

		expect(body.retention_days).toBe(0);
	});
});
