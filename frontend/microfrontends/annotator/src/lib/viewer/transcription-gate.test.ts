/**
 * Transcription is offered PER the class's declaration — the model finally answers "is
 * transcription an attribute?" (it is neither a tool nor an attribute: it is the row's `text`
 * facet, declared per class). Before this, the inspector offered a text editor on every row of
 * every task, detection boxes included.
 */

import { describe, expect, it, vi } from 'vitest';

vi.mock('svelte-sonner', () => ({
	toast: {
		success: () => {},
		error: () => {},
		message: () => {},
		warning: () => {},
		info: () => {},
	},
}));
vi.mock('./remote/assist.remote', () => ({
	requestAssist: async () => ({ ok: true, data: { shapes: [], source: '', dropped: [] } }),
	assistProducers: async () => [],
}));
vi.mock('./remote/jobs.remote', () => ({
	submitJob: async () => ({ status: 'unsupported' }),
	jobStatus: async () => null,
}));

import { AnnotatorController } from './annotator.svelte.js';

describe('offersTranscription', () => {
	it('an unconstrained canvas (no task) offers it everywhere — the historical behaviour', () => {
		const c = new AnnotatorController();
		expect(c.offersTranscription('anything')).toBe(true);
		expect(c.offersTranscription('')).toBe(true);
	});

	it('a constrained task offers it exactly where declared', () => {
		const c = new AnnotatorController();
		c.classTranscribe = { paragraph: true, stamp: false };
		expect(c.offersTranscription('paragraph')).toBe(true);
		expect(c.offersTranscription('stamp')).toBe(false);
	});

	it('a label NO class covers stays editable — no declaration, no refusal', () => {
		// An unlabeled draft or a document row must not lose its text editor because OTHER
		// classes declared theirs.
		const c = new AnnotatorController();
		c.classTranscribe = { stamp: false };
		expect(c.offersTranscription('')).toBe(true);
		expect(c.offersTranscription('mystery')).toBe(true);
	});
});
