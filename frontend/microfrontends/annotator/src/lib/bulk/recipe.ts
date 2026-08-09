/**
 * Act-first "＋ column" — the pure half (open-bulk phase 3).
 *
 * The user types an ACTION; everything contractual is DERIVED from it, never demanded up
 * front (the audited dynamism bar, spec §5): the column's name comes from the words of the
 * action, the declaration is a tag-tooled transcribing class appended to the task's ontology
 * (one silent PATCH), and the value physically lands as a `tag` row whose `text` is the cell.
 * Progressive typing starts here at `free` — tightening to an enum later is an ontology edit,
 * not a migration.
 */

import type { Project } from '$lib/projects/types';

export type Ontology = Project['ontology'];

/** A recipe column IS a tag-tooled class that transcribes: drawn as a whole-item tag, its rows
 *  carry text — exactly the projection the grid renders as a spreadsheet column. Classification
 *  chips (tag-tooled, no text) stay in the Tags column; this predicate separates the two. */
export function recipeColumns(ontology: Ontology | undefined): string[] {
	if (!ontology) return [];
	return ontology.classes
		.filter((c) => c.transcribe && c.tools.length === 1 && c.tools[0] === 'tag')
		.map((c) => c.name);
}

/** Words that carry no column-name information — trimmed from the action, never from data. */
const STOPWORDS = new Set([
	'a',
	'an',
	'and',
	'are',
	'does',
	'for',
	'from',
	'in',
	'is',
	'it',
	'of',
	'on',
	'or',
	'the',
	'this',
	'to',
	'what',
	'which',
	'with',
]);

/**
 * Name the column from the action — `"which century is this document from?"` → `"century"`.
 * `{{references}}` are dependency edges, not name material, so they are stripped first. The
 * name must exist (fallback `column`) and be unique in the ontology (suffix `-2`, `-3`, …),
 * because a duplicate class name is a validation error server-side.
 */
export function deriveColumnName(action: string, existing: string[]): string {
	const words = action
		.replace(/\{\{[^}]*\}\}/g, ' ')
		.toLowerCase()
		.replace(/[^a-z0-9åäö\s-]/g, ' ')
		.split(/\s+/)
		.filter((w) => w.length > 1 && !STOPWORDS.has(w));
	const stem = words.slice(0, 3).join('-').slice(0, 32).replace(/-+$/, '') || 'column';
	const taken = new Set(existing);
	if (!taken.has(stem)) return stem;
	for (let n = 2; ; n++) {
		const candidate = `${stem}-${n}`;
		if (!taken.has(candidate)) return candidate;
	}
}

/** The `{{column}}` mentions in the action — the recipe's dependency edges, derived (never a
 *  separate wiring step). Order of first mention, deduplicated. */
export function referencesOf(action: string): string[] {
	const refs: string[] = [];
	for (const match of action.matchAll(/\{\{\s*([^}]+?)\s*\}\}/g)) {
		const name = match[1];
		if (name && !refs.includes(name)) refs.push(name);
	}
	return refs;
}

/** The ontology with the derived class appended — the wire shape `updateOntology` PATCHes.
 *  Pure: the caller owns optimistic state and the re-fetch. */
export function withRecipeClass(ontology: Ontology, name: string): Ontology {
	return {
		...ontology,
		classes: [
			...ontology.classes,
			{
				name,
				colour: null,
				tools: ['tag'],
				transcribe: true,
				attributes: [],
				required: false,
			},
		],
	};
}
