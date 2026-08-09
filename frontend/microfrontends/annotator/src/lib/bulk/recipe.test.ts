/** Act-first add-column: everything contractual DERIVES from the typed action — pinned pure. */

import { describe, expect, it } from 'vitest';
import { deriveColumnName, recipeColumns, referencesOf, withRecipeClass } from './recipe.js';
import type { Ontology } from './recipe.js';

const ontology = (classes: Ontology['classes']): Ontology => ({
	kind: '',
	modality: 'image',
	classes,
	relations: [],
	allow_empty: false,
});

const klass = (name: string, tools: string[], transcribe = false): Ontology['classes'][number] => ({
	name,
	colour: null,
	tools,
	transcribe,
	attributes: [],
	required: false,
});

describe('recipeColumns', () => {
	it('a recipe column is a tag-tooled class that transcribes — chips and regions stay out', () => {
		const o = ontology([
			klass('paragraph', ['bbox'], true), // transcribing REGION → the Text column, not a recipe
			klass('damaged', ['tag']), // classification chip → the Tags column
			klass('century', ['tag'], true), // the derived recipe column
		]);
		expect(recipeColumns(o)).toEqual(['century']);
	});

	it('no ontology, no columns', () => {
		expect(recipeColumns(undefined)).toEqual([]);
	});
});

describe('deriveColumnName', () => {
	it('names from the action words, stopwords and references stripped', () => {
		expect(deriveColumnName('which century is this document from?', [])).toBe('century-document');
		expect(deriveColumnName('translate {{transcription}} to modern Swedish', [])).toBe(
			'translate-modern-swedish',
		);
	});

	it('an action of pure stopwords still names SOMETHING', () => {
		expect(deriveColumnName('is this it?', [])).toBe('column');
	});

	it('uniquifies against the existing taxonomy — a duplicate class name is a server error', () => {
		expect(deriveColumnName('century', ['century'])).toBe('century-2');
		expect(deriveColumnName('century', ['century', 'century-2'])).toBe('century-3');
	});
});

describe('referencesOf', () => {
	it('the {{mentions}} ARE the dependency edges, first-mention order, deduplicated', () => {
		expect(referencesOf('given {{transcription}} and {{script}}, use {{transcription}}')).toEqual([
			'transcription',
			'script',
		]);
		expect(referencesOf('no references here')).toEqual([]);
	});
});

describe('withRecipeClass', () => {
	it('appends the derived declaration — tag-tooled, transcribing, unconstrained otherwise', () => {
		const before = ontology([klass('paragraph', ['bbox'], true)]);
		const after = withRecipeClass(before, 'century');
		expect(after.classes.map((c) => c.name)).toEqual(['paragraph', 'century']);
		expect(after.classes[1]).toMatchObject({ tools: ['tag'], transcribe: true, required: false });
		// Pure: the input ontology is untouched (the caller owns optimistic state).
		expect(before.classes).toHaveLength(1);
	});
});
