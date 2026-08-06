/**
 * Leaving the canvas.
 *
 * Reported: "in anno i can't even navigate between the claimed labeling items… and why does this
 * button exist in the sidebar of annotate 'Back to document selection' and it goes back to
 * /annotator/?dataset=transcripts_v2 and not the list of tasks I have".
 *
 * Exactly right. The exit always went to the corpus browser, including when the canvas had been
 * opened FROM a labeling queue — so finishing an item dropped you into a document gallery with your
 * queue gone. The canvas was carrying `task=` the whole time and discarding it.
 */

import { describe, expect, it } from 'vitest';

import { exitHref } from './exit-target';

const params = (qs: string) => new URLSearchParams(qs);

describe('leaving a canvas opened from a labeling task', () => {
	it('returns to THAT task queue, not the corpus browser', () => {
		const href = exitHref(params('dataset=transcripts_v2&keys=a/1/1&task=t9&project=bind86'));

		expect(href).toBe('/projects/bind86');
	});

	it('prefers the queue even though a dataset is also present', () => {
		// Both are always in the URL for a task-opened canvas — the dataset is how the ITEM resolves,
		// not where the person was working. Picking the dataset is what produced the report.
		const href = exitHref(params('dataset=transcripts_v2&task=t9&project=bind86'));

		expect(href).not.toContain('dataset');
	});

	it('encodes a project id that needs it', () => {
		expect(exitHref(params('project=a%20b'))).toBe('/projects/a%20b');
	});

	it('respects the zone base', () => {
		expect(exitHref(params('project=bind86'), '/annotator')).toBe('/annotator/projects/bind86');
	});
});

describe('leaving a canvas opened from the corpus browser', () => {
	it('keeps the dataset, so exit does not silently switch corpus', () => {
		expect(exitHref(params('dataset=vasa&keys=a/1/1'))).toBe('/?dataset=vasa');
	});

	it('goes to the landing when there is no dataset either', () => {
		expect(exitHref(params(''))).toBe('/');
	});

	it('ignores a task id with no project — it cannot address a queue page', () => {
		// `task` alone identifies the item, not the list. Building `/projects/<task>` from it would
		// 404, which is worse than the browser.
		expect(exitHref(params('dataset=vasa&task=t9'))).toBe('/?dataset=vasa');
	});
});
