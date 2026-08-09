/**
 * The designer preview is a PURE projection of the draft — the same derivations the real
 * workspace makes from the ontology (toolbar from draw union, chips from spatial classes, the
 * classification bar from tags, span hotkeys, inspector rows, surviving relations). Pinned
 * separately so the preview cannot quietly diverge from the draft it claims to show.
 */

import { describe, expect, it } from 'vitest';
import { previewModel } from './task-preview.js';
import type { TaskDraft } from './task-yaml.js';

const cls = (over: Partial<TaskDraft['classes'][number]>): TaskDraft['classes'][number] => ({
	name: 'x',
	draw: [],
	span: false,
	tag: false,
	transcribe: false,
	required: false,
	fields: [],
	...over,
});

const draft = (over: Partial<TaskDraft>): TaskDraft => ({
	kind: '',
	classes: [],
	relations: [],
	allowEmpty: undefined,
	...over,
});

describe('previewModel', () => {
	it('projects every surface the ontology drives', () => {
		const m = previewModel(
			draft({
				classes: [
					cls({
						name: 'paragraph',
						draw: ['polygon', 'bbox'],
						transcribe: true,
						required: true,
						fields: [{ name: 'order', type: 'int', options: [], required: true }],
					}),
					cls({ name: 'person', span: true }),
					cls({ name: 'damaged', tag: true }),
				],
				relations: [
					{
						name: 'annotates',
						from: ['person'],
						to: ['paragraph'],
						directed: true,
						required: false,
					},
				],
			}),
		);
		expect(m.draw).toEqual(['polygon', 'bbox']);
		expect(m.labels).toEqual([{ name: 'paragraph', required: true }]);
		expect(m.tags).toEqual(['damaged']);
		expect(m.spans).toEqual(['person']);
		expect(m.inspectors).toEqual([
			{
				class: 'paragraph',
				transcribe: true,
				fields: [{ name: 'order', type: 'int', options: [], required: true }],
			},
		]);
		expect(m.relations).toEqual([{ name: 'annotates', from: ['person'], to: ['paragraph'] }]);
		expect(m.empty).toBe(false);
	});

	it('the toolbar is a UNION without duplicates, in first-seen order', () => {
		const m = previewModel(
			draft({
				classes: [cls({ name: 'a', draw: ['bbox'] }), cls({ name: 'b', draw: ['bbox', 'mask'] })],
			}),
		);
		expect(m.draw).toEqual(['bbox', 'mask']);
	});

	it('a relation loses its strip when an endpoint stops existing — the payload rule, mirrored', () => {
		const m = previewModel(
			draft({
				classes: [cls({ name: 'value', draw: ['bbox'] })],
				relations: [
					{ name: 'answers', from: ['question'], to: ['value'], directed: true, required: false },
				],
			}),
		);
		expect(m.relations).toEqual([]);
	});

	it('unnamed classes are invisible, and an all-unnamed draft reads as EMPTY', () => {
		const m = previewModel(draft({ classes: [cls({ name: '  ', draw: ['bbox'] })] }));
		expect(m.empty).toBe(true);
		expect(m.draw).toEqual([]);
	});
});
