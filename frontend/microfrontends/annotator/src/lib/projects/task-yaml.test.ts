/**
 * The YAML task definition — the human vocabulary over the wire ontology.
 *
 * The wire's `tools` conflates drawing geometry, span capability and tag-ness, and cannot say
 * "carries transcription" at all — the YAML says each in its own place (draw / span / tag /
 * transcribe). These tests pin the mapping both ways, the strictness (unknown keys are ERRORS,
 * mirroring the server's extra="forbid" — a typo must never silently drop a constraint), and
 * the ghost-edge filter on relations.
 */

import { describe, expect, it } from 'vitest';
import { PROJECT_TEMPLATES } from './templates.js';
import {
	type TaskDraft,
	draftClassFromWire,
	draftToOntology,
	draftToYaml,
	parseTaskYaml,
} from './task-yaml.js';

const DRAFT: TaskDraft = {
	kind: 'ocr-layout',
	classes: [
		{
			name: 'paragraph',
			draw: ['polygon', 'bbox'],
			span: false,
			tag: false,
			transcribe: true,
			required: true,
			fields: [
				{ name: 'order', type: 'int', options: [], required: true },
				{ name: 'script', type: 'enum', options: ['blackletter', 'cursive'], required: false },
			],
		},
		{
			name: 'person',
			draw: [],
			span: true,
			tag: false,
			transcribe: false,
			required: false,
			fields: [],
		},
		{
			name: 'damaged',
			draw: [],
			span: false,
			tag: true,
			transcribe: false,
			required: false,
			fields: [],
		},
	],
	relations: [
		{ name: 'annotates', from: ['person'], to: ['paragraph'], directed: true, required: false },
	],
	allowEmpty: undefined,
};

describe('draft → YAML → draft', () => {
	it('round-trips every capability, field and relation', () => {
		const { draft, errors } = parseTaskYaml(draftToYaml(DRAFT));
		expect(errors).toEqual([]);
		expect(draft).toEqual(DRAFT);
	});

	it('the YAML separates what the wire conflates', () => {
		const yaml = draftToYaml(DRAFT);
		expect(yaml).toContain('task: ocr-layout');
		expect(yaml).toContain('transcribe: true');
		expect(yaml).toContain('span: true');
		expect(yaml).toContain('tag: true');
		// The wire's dialect never leaks into the human surface.
		expect(yaml).not.toContain('tools:');
		expect(yaml).not.toContain('attributes:');
		expect(yaml).not.toContain('from_classes');
	});
});

describe('draft → wire ontology', () => {
	it('maps the vocabulary back onto tools/attributes/relations', () => {
		const ontology = draftToOntology(DRAFT);
		expect(ontology.kind).toBe('ocr-layout');
		expect(ontology.classes[0]).toMatchObject({
			name: 'paragraph',
			tools: ['polygon', 'bbox'],
			transcribe: true,
			required: true,
		});
		expect(ontology.classes[1]).toMatchObject({ name: 'person', tools: ['text'] });
		expect(ontology.classes[2]).toMatchObject({ name: 'damaged', tools: ['tag'] });
		const attrs = (ontology.classes[0] as { attributes: Record<string, unknown>[] }).attributes;
		expect(attrs[0]).toEqual({ name: 'order', type: 'int', required: true });
		expect(attrs[1]).toEqual({
			name: 'script',
			type: 'enum',
			choices: ['blackletter', 'cursive'],
			required: false,
		});
		expect(ontology.relations[0]).toEqual({
			name: 'annotates',
			from_classes: ['person'],
			to_classes: ['paragraph'],
		});
	});

	it('drops classes that can produce nothing, and the ghost edges they leave', () => {
		const ontology = draftToOntology({
			...DRAFT,
			classes: [
				{ ...DRAFT.classes[0]!, name: '  ' }, // unnamed → dropped
				DRAFT.classes[1]!, // person survives
				{ ...DRAFT.classes[2]!, tag: false }, // no capability at all → dropped
			],
		});
		expect(ontology.classes.map((c) => c.name)).toEqual(['person']);
		// `annotates` named paragraph, which no longer exists — the edge must not reach the server.
		expect(ontology.relations).toEqual([]);
	});
});

describe('wire → draft (seeding from a template)', () => {
	it('splits tools into draw/span/tag and carries transcribe', () => {
		const ocr = PROJECT_TEMPLATES.find((t) => t.id === 'ocr-layout')!;
		const paragraph = draftClassFromWire(ocr.ontology.classes[1]!);
		expect(paragraph).toMatchObject({
			draw: ['polygon', 'bbox'],
			span: false,
			tag: false,
			transcribe: true,
		});
		const person = draftClassFromWire(ocr.ontology.classes[4]!);
		expect(person).toMatchObject({ draw: [], span: true, tag: false, transcribe: false });
		const damaged = draftClassFromWire(ocr.ontology.classes[6]!);
		expect(damaged).toMatchObject({ draw: [], span: false, tag: true });
	});
});

describe('strict parsing — a typo is an error, never a silent drop', () => {
	it('unknown keys are named at every level', () => {
		const { errors } = parseTaskYaml(
			['labels:', '  - name: a', '    draw: [bbox]', '    atributes: []', 'allowempty: true'].join(
				'\n',
			),
		);
		expect(errors.some((e) => e.includes('atributes'))).toBe(true);
		expect(errors.some((e) => e.includes('allowempty'))).toBe(true);
	});

	it('an unknown draw tool is refused with the legal list', () => {
		const { errors } = parseTaskYaml('labels:\n  - name: a\n    draw: [rectangle]');
		expect(errors[0]).toContain('rectangle');
		expect(errors[0]).toContain('bbox');
	});

	it('broken YAML reports the parser message instead of throwing', () => {
		const { errors } = parseTaskYaml('labels: [unclosed');
		expect(errors).toHaveLength(1);
	});

	it('an empty document parses to an empty draft with no errors', () => {
		const { draft, errors } = parseTaskYaml('');
		expect(errors).toEqual([]);
		expect(draft.classes).toEqual([]);
	});

	it('options imply enum; a declared enum without options is refused', () => {
		const ok = parseTaskYaml(
			'labels:\n  - name: a\n    draw: [bbox]\n    fields:\n      - name: s\n        options: [x, y]',
		);
		expect(ok.errors).toEqual([]);
		expect(ok.draft.classes[0]?.fields[0]).toMatchObject({ type: 'enum', options: ['x', 'y'] });
		const bad = parseTaskYaml(
			'labels:\n  - name: a\n    fields:\n      - name: s\n        type: enum',
		);
		expect(bad.errors.some((e) => e.includes('options'))).toBe(true);
	});
});
