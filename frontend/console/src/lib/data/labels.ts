/**
 * LABELS (ADR 0211): names a project defines and an object's owner attaches to a table or a
 * namespace of that project, as a bare marker, a free text or one of the definition's allowed values.
 * A label authorizes nothing: it is something to read, filter and find objects by.
 *
 * The shapes are the catalog's generated contract; `satisfies` keeps each schema's keys in lockstep
 * with it, so a field the catalog adds or renames stops compiling here. Which warehouse belongs to no
 * project is the catalog's setting, not the console's: an object there is answered 400 by its label
 * doors, and the console reads that answer rather than a copy of the name.
 */
import * as v from 'valibot';
import type { components } from '#lib/api/generated/catalog.ts';

type DefinitionWire = components['schemas']['LabelDefinition'];
type DefineWire = components['schemas']['LabelDefinitionRequest'];
type ObjectLabelsWire = components['schemas']['ObjectLabelsResponse'];
type LabelledWire = components['schemas']['LabelledObjectsResponse'];

/** The two kinds of catalog object a label is attached to. */
export const LABEL_SCOPES = ['namespace', 'table'] as const;
export type LabelScope = (typeof LABEL_SCOPES)[number];

/** What a label carries: nothing (a marker), a text of its own, or one of the allowed values. */
export const LABEL_KINDS = ['marker', 'free_text', 'enumerated'] as const;
export type LabelKind = (typeof LABEL_KINDS)[number];

/** The kind as a person reads it. */
export const KIND_NAMES: Record<LabelKind, string> = {
	marker: 'marker',
	free_text: 'free text',
	enumerated: 'one of a list',
};

export const LabelDefinitionSchema = v.object({
	uuid: v.string(),
	project: v.string(),
	/** The creation of the project that defined it (ADR 0245). */
	project_uuid: v.string(),
	name: v.string(),
	description: v.optional(v.nullable(v.string())),
	scope: v.array(v.picklist(LABEL_SCOPES)),
	kind: v.picklist(LABEL_KINDS),
	allowed_values: v.optional(v.nullable(v.array(v.string()))),
	defined_by: v.optional(v.nullable(v.string())),
	defined_at: v.string(),
} satisfies Record<keyof DefinitionWire, unknown>);
export type LabelDefinition = v.InferOutput<typeof LabelDefinitionSchema>;

/** The body of a define: a new label, or the definition its name holds, evolved. */
export const DefineLabelSchema = v.object({
	name: v.string(),
	description: v.optional(v.nullable(v.string())),
	scope: v.array(v.picklist(LABEL_SCOPES)),
	kind: v.picklist(LABEL_KINDS),
	allowed_values: v.optional(v.nullable(v.array(v.string()))),
} satisfies Record<keyof DefineWire, unknown>);
export type DefineLabel = v.InferOutput<typeof DefineLabelSchema>;

/** The labels one object carries, each with its value (null for a marker), and on a describe the
 *  definitions its project holds for its kind (`available`): what may be attached to it, read at
 *  `can_get_metadata` on the object, so an owner who is no member of the project reads it too. */
export const ObjectLabelsSchema = v.object({
	kind: v.picklist(LABEL_SCOPES),
	id: v.string(),
	uuid: v.string(),
	labels: v.record(v.string(), v.nullable(v.string())),
	available: v.optional(v.nullable(v.array(LabelDefinitionSchema))),
} satisfies Record<keyof ObjectLabelsWire, unknown>);
export type ObjectLabels = v.InferOutput<typeof ObjectLabelsSchema>;
export type Labels = ObjectLabels['labels'];

/** A project's labelled objects the caller may see, every page joined (`catalogListing`). */
export const LabelledObjectsSchema = v.object({
	project: v.string(),
	objects: v.array(ObjectLabelsSchema),
	page_token: v.optional(v.nullable(v.string())),
} satisfies Record<keyof LabelledWire, unknown>);
export type LabelledObjects = v.InferOutput<typeof LabelledObjectsSchema>;

/** One label as a chip reads: `name`, or `name=value`. */
export function labelChip(name: string, value: string | null): string {
	return value === null ? name : `${name}=${value}`;
}

/** An object's labels as chips, by name. */
export function labelChips(labels: Labels): string[] {
	return Object.keys(labels)
		.sort()
		.map((name) => labelChip(name, labels[name] ?? null));
}

/** The definitions a `kind` of object may carry, by name. */
export function definitionsFor(
	definitions: readonly LabelDefinition[],
	kind: LabelScope,
): LabelDefinition[] {
	return definitions
		.filter((d) => d.scope.includes(kind))
		.sort((a, b) => a.name.localeCompare(b.name));
}

/** The key one labelled object is indexed by: a table and a namespace may share an id's text. */
export function objectKey(kind: LabelScope, id: string): string {
	return `${kind}:${id}`;
}

/** The URL parameter a catalog list keeps its label filter in. */
export const LABEL_PARAM = 'label';

/** The label the URL filters by, or `''` for any label. */
export function labelFilterOf(url: {
	readonly searchParams: { get(name: string): string | null };
}): string {
	return url.searchParams.get(LABEL_PARAM) ?? '';
}

/** One choice of the label filter's select. */
export type LabelOption = { value: string; label: string; disabled?: boolean };

/** The label filter's choices: every label, then each name some object the caller can see carries.
 *  A filter naming none of them stays on screen, as a choice that cannot be picked. */
export function labelOptions(names: readonly string[], current: string): LabelOption[] {
	const sorted = [...new Set(names)].sort();
	const options: LabelOption[] = [
		{ value: '', label: 'any label' },
		...sorted.map((n) => ({ value: n, label: n })),
	];
	if (current && !sorted.includes(current))
		options.push({ value: current, label: `${current} (on nothing you can see)`, disabled: true });
	return options;
}
