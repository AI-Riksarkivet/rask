/**
 * The gallery's templates are CONTRACTS — each must be an ontology the server would accept and
 * the workspace can drive. The rules mirrored here are the server model's own hard errors
 * (duplicate names, enum/choices coherence) plus the two internal-consistency rules relation
 * declarations depend on. A template that fails here would fail at project create, at runtime,
 * in front of the person the gallery just promised a working task.
 */

import { describe, expect, it } from 'vitest';

import { PROJECT_TEMPLATES, templateById } from './templates';

const TOOLS = new Set([
	'bbox',
	'polygon',
	'mask',
	'keypoint',
	'polyline',
	'segment',
	'tag',
	'text',
]);

describe('the template gallery', () => {
	it('every template is internally consistent the way the server demands', () => {
		for (const t of PROJECT_TEMPLATES) {
			const names = t.ontology.classes.map((c) => c.name);
			expect(new Set(names).size, `${t.id}: duplicate class names`).toBe(names.length);
			for (const c of t.ontology.classes) {
				expect(
					c.tools.length,
					`${t.id}/${c.name}: a template class declares its tools`,
				).toBeGreaterThan(0);
				for (const tool of c.tools)
					expect(TOOLS.has(tool), `${t.id}/${c.name}: unknown tool ${tool}`).toBe(true);
				for (const a of c.attributes ?? []) {
					if (a.type === 'enum')
						expect(
							a.choices?.length ?? 0,
							`${t.id}/${c.name}/${a.name}: enum needs choices`,
						).toBeGreaterThan(0);
					else
						expect(a.choices ?? [], `${t.id}/${c.name}/${a.name}: choices only on enum`).toEqual(
							[],
						);
				}
			}
			for (const r of t.ontology.relations ?? []) {
				for (const end of [...r.from_classes, ...r.to_classes])
					expect(names, `${t.id}/${r.name}: relation names undeclared class ${end}`).toContain(end);
			}
		}
	});

	it('ids are unique and resolvable', () => {
		const ids = PROJECT_TEMPLATES.map((t) => t.id);
		expect(new Set(ids).size).toBe(ids.length);
		expect(templateById('ocr-layout')?.name).toContain('OCR');
		expect(templateById('nope')).toBeUndefined();
	});

	it('the composite template exercises EVERY facet the workspace composes', () => {
		// The point of the gallery: one ontology drives regions + per-class tools + typed
		// attributes + spans + tags + a relation, with zero template-specific code.
		const t = templateById('ocr-layout')!;
		const tools = new Set(t.ontology.classes.flatMap((c) => c.tools));
		expect(tools).toEqual(new Set(['bbox', 'polygon', 'text', 'tag']));
		expect(
			t.ontology.classes.some((c) => (c.attributes ?? []).some((a) => a.type === 'enum')),
		).toBe(true);
		expect(t.ontology.relations?.length).toBe(1);
	});
});
