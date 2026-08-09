/**
 * The task definition as YAML — the human-readable authoring surface over `LabelOntology`.
 *
 * The form editor exposes the common edits; YAML is the FULL-POWER surface (typed fields,
 * relations, allow_empty) and the exchange format — copy it out, version it, paste it back.
 * The WIRE stays JSON: this module maps between the YAML vocabulary and the ontology document,
 * and nothing downstream knows YAML existed.
 *
 * The vocabulary is deliberately not the wire's. The wire conflates three different things
 * under `tools`: HOW a region is drawn (bbox/polygon/mask/segment), whether the class marks
 * SPANS into text (`text`), and whether it is a whole-item TAG (`tag`) — and it cannot say
 * "carries transcription" at all. The YAML says each thing in its own place:
 *
 *   task: ocr-layout
 *   labels:
 *     - name: paragraph
 *       draw: [polygon, bbox]     # geometry on the canvas
 *       transcribe: true          # each region carries transcribed text (the `text` facet)
 *       required: true
 *       fields:                   # typed per-region fields ("attributes" on the wire)
 *         - name: order
 *           type: int
 *           required: true
 *         - name: script
 *           options: [blackletter, cursive, print]   # options ⇒ a closed choice
 *     - name: person
 *       span: true                # marks ranges IN the text (NER)
 *     - name: damaged
 *       tag: true                 # a whole-item choice (classification chip)
 *   relations:
 *     - name: annotates
 *       from: [marginalia]
 *       to: [paragraph]
 *   allow_empty: false
 *
 * Parsing is STRICT, mirroring the server's `extra="forbid"`: an unknown key is an error, not
 * a shrug — `atributes:` silently vanishing is exactly the typo-drops-a-constraint failure the
 * server model exists to refuse.
 */

import { parse, stringify } from 'yaml';

/** One label, in the editor's working vocabulary. */
export interface DraftClass {
	name: string;
	/** Spatial drawing primitives (bbox/polygon/mask/segment). */
	draw: string[];
	/** Marks character ranges IN the text — the NER/span capability (`text` tool on the wire). */
	span: boolean;
	/** A whole-item choice — the classification capability (`tag` tool on the wire). */
	tag: boolean;
	/** Regions of this class carry transcribed text (the row's `text` facet). */
	transcribe: boolean;
	required: boolean;
	fields: DraftField[];
}

export interface DraftField {
	name: string;
	type: 'free' | 'int' | 'enum' | 'bool';
	options: string[];
	required: boolean;
}

export interface DraftRelation {
	name: string;
	from: string[];
	to: string[];
	directed: boolean;
	required: boolean;
}

/** The whole editable task definition — ONE object, whichever view (form or YAML) edits it. */
export interface TaskDraft {
	kind: string;
	classes: DraftClass[];
	relations: DraftRelation[];
	allowEmpty: boolean | undefined;
}

export const DRAW_TOOLS = ['bbox', 'polygon', 'mask', 'segment'] as const;

const emptyClass = (): DraftClass => ({
	name: '',
	draw: [],
	span: false,
	tag: false,
	transcribe: false,
	required: false,
	fields: [],
});

const FIELD_TYPES = new Set(['free', 'int', 'enum', 'bool']);

/** The type-predicate narrow — `Set.has` alone doesn't narrow a string to the union. */
function isFieldType(value: string): value is DraftField['type'] {
	return FIELD_TYPES.has(value);
}

/** Narrow a wire type string to the closed field-type union — 'free' for anything unknown,
 *  because a wire value is data, not a proof, and an `as` cast here would silently launder
 *  whatever a stored ontology happens to carry. */
function fieldType(type: string | undefined): DraftField['type'] {
	return type !== undefined && isFieldType(type) ? type : 'free';
}

/** Split a wire `tools` list into the vocabulary's three capabilities. */
export function draftClassFromWire(c: {
	name: string;
	tools: string[];
	transcribe?: boolean;
	required?: boolean;
	attributes?: { name: string; type?: string; choices?: string[]; required?: boolean }[];
}): DraftClass {
	return {
		name: c.name,
		draw: c.tools.filter((t) => t !== 'text' && t !== 'tag'),
		span: c.tools.includes('text'),
		tag: c.tools.includes('tag'),
		transcribe: c.transcribe ?? false,
		required: c.required ?? false,
		fields: (c.attributes ?? []).map((a) => ({
			name: a.name,
			type: a.choices?.length ? 'enum' : fieldType(a.type),
			options: a.choices ? [...a.choices] : [],
			required: a.required ?? false,
		})),
	};
}

/** The wire ontology's class/relation rows, as the create call sends them. */
export interface WireClass {
	name: string;
	tools: string[];
	transcribe?: boolean;
	required: boolean;
	attributes: { name: string; type: string; choices?: string[]; required: boolean }[];
}
export interface WireRelation {
	name: string;
	from_classes: string[];
	to_classes: string[];
	directed?: boolean;
	required?: boolean;
}

/** The wire ontology document a draft creates — classes with empty names or NO capability are
 *  dropped (a label that can produce nothing), and relations survive only while both endpoints
 *  exist (a ghost edge would fail the whole create server-side). */
export function draftToOntology(draft: TaskDraft): {
	kind?: string;
	classes: WireClass[];
	relations: WireRelation[];
	allow_empty?: boolean;
} {
	const classes = draft.classes
		.filter((c) => c.name.trim() !== '' && (c.draw.length > 0 || c.span || c.tag))
		.map((c) => ({
			name: c.name.trim(),
			tools: [...c.draw, ...(c.span ? ['text'] : []), ...(c.tag ? ['tag'] : [])],
			...(c.transcribe ? { transcribe: true } : {}),
			required: c.required,
			attributes: c.fields
				.filter((f) => f.name.trim() !== '')
				.map((f) => ({
					name: f.name.trim(),
					// `options` present ⇒ enum, whatever `type` says — one signal, not two to disagree.
					type: f.options.length > 0 ? 'enum' : f.type,
					...(f.options.length > 0 ? { choices: f.options } : {}),
					required: f.required,
				})),
		}));
	const names = new Set(classes.map((c) => c.name));
	return {
		...(draft.kind ? { kind: draft.kind } : {}),
		classes,
		relations: draft.relations
			.filter((r) => [...r.from, ...r.to].every((n) => names.has(n)))
			.map((r) => ({
				name: r.name,
				from_classes: r.from,
				to_classes: r.to,
				...(r.directed ? {} : { directed: false }),
				...(r.required ? { required: true } : {}),
			})),
		...(draft.allowEmpty !== undefined ? { allow_empty: draft.allowEmpty } : {}),
	};
}

/** Serialize a draft as the YAML the annotator speaks — omitting every default, so the text
 *  reads as the task's actual declarations rather than a form dump. */
export function draftToYaml(draft: TaskDraft): string {
	const doc: Record<string, unknown> = {};
	if (draft.kind) doc.task = draft.kind;
	doc.labels = draft.classes.map((c) => ({
		name: c.name,
		...(c.draw.length ? { draw: c.draw } : {}),
		...(c.span ? { span: true } : {}),
		...(c.tag ? { tag: true } : {}),
		...(c.transcribe ? { transcribe: true } : {}),
		...(c.required ? { required: true } : {}),
		...(c.fields.length
			? {
					fields: c.fields.map((f) => ({
						name: f.name,
						...(f.options.length
							? { options: f.options }
							: f.type !== 'free'
								? { type: f.type }
								: {}),
						...(f.required ? { required: true } : {}),
					})),
				}
			: {}),
	}));
	if (draft.relations.length) {
		doc.relations = draft.relations.map((r) => ({
			name: r.name,
			from: r.from,
			to: r.to,
			...(r.directed ? {} : { directed: false }),
			...(r.required ? { required: true } : {}),
		}));
	}
	if (draft.allowEmpty !== undefined) doc.allow_empty = draft.allowEmpty;
	return stringify(doc);
}

const KNOWN_TOP = new Set(['task', 'labels', 'relations', 'allow_empty']);
const KNOWN_LABEL = new Set(['name', 'draw', 'span', 'tag', 'transcribe', 'required', 'fields']);
const KNOWN_FIELD = new Set(['name', 'type', 'options', 'required']);
const KNOWN_RELATION = new Set(['name', 'from', 'to', 'directed', 'required']);

/** Parse the YAML back into a draft. Returns errors instead of throwing — the editor shows
 *  them in place — and is STRICT: unknown keys, wrong shapes and unknown draw tools are named,
 *  never dropped. */
export function parseTaskYaml(text: string): { draft: TaskDraft; errors: string[] } {
	const errors: string[] = [];
	const draft: TaskDraft = { kind: '', classes: [], relations: [], allowEmpty: undefined };
	let doc: unknown;
	try {
		doc = parse(text);
	} catch (e) {
		return { draft, errors: [e instanceof Error ? e.message : String(e)] };
	}
	if (doc === null || doc === undefined) return { draft, errors };
	if (typeof doc !== 'object' || Array.isArray(doc)) {
		return { draft, errors: ['the document must be a mapping (task / labels / relations)'] };
	}
	const top = doc as Record<string, unknown>;
	checkKeys(top, KNOWN_TOP, 'the document', errors);
	if (top.task !== undefined) {
		if (typeof top.task === 'string') draft.kind = top.task;
		else errors.push('`task` must be a string');
	}
	if (top.allow_empty !== undefined) {
		if (typeof top.allow_empty === 'boolean') draft.allowEmpty = top.allow_empty;
		else errors.push('`allow_empty` must be true or false');
	}
	for (const [i, raw] of asList(top.labels, 'labels', errors).entries()) {
		const label = asMapping(raw, `labels[${i}]`, errors);
		if (label) draft.classes.push(parseLabel(label, `labels[${i}]`, errors));
	}
	for (const [i, raw] of asList(top.relations, 'relations', errors).entries()) {
		const rel = asMapping(raw, `relations[${i}]`, errors);
		if (rel) draft.relations.push(parseRelation(rel, `relations[${i}]`, errors));
	}
	return { draft, errors };
}

function parseLabel(label: Record<string, unknown>, where: string, errors: string[]): DraftClass {
	checkKeys(label, KNOWN_LABEL, where, errors);
	const c = emptyClass();
	c.name = requireName(label.name, where, errors);
	for (const t of asList(label.draw, `${where}.draw`, errors)) {
		if (typeof t === 'string' && (DRAW_TOOLS as readonly string[]).includes(t)) c.draw.push(t);
		else errors.push(`${where}.draw: \`${String(t)}\` is not one of ${DRAW_TOOLS.join(', ')}`);
	}
	c.span = asBool(label.span, `${where}.span`, errors);
	c.tag = asBool(label.tag, `${where}.tag`, errors);
	c.transcribe = asBool(label.transcribe, `${where}.transcribe`, errors);
	c.required = asBool(label.required, `${where}.required`, errors);
	for (const [j, raw] of asList(label.fields, `${where}.fields`, errors).entries()) {
		const field = asMapping(raw, `${where}.fields[${j}]`, errors);
		if (field) c.fields.push(parseField(field, `${where}.fields[${j}]`, errors));
	}
	return c;
}

function parseField(field: Record<string, unknown>, where: string, errors: string[]): DraftField {
	checkKeys(field, KNOWN_FIELD, where, errors);
	const options = asList(field.options, `${where}.options`, errors).map(String);
	let type: DraftField['type'] = options.length > 0 ? 'enum' : 'free';
	if (field.type !== undefined) {
		if (typeof field.type === 'string' && isFieldType(field.type)) type = field.type;
		else
			errors.push(
				`${where}.type: \`${String(field.type)}\` is not one of ${[...FIELD_TYPES].join(', ')}`,
			);
	}
	if (type === 'enum' && options.length === 0) errors.push(`${where}: type enum needs \`options\``);
	return {
		name: requireName(field.name, where, errors),
		type,
		options,
		required: asBool(field.required, `${where}.required`, errors),
	};
}

function parseRelation(
	rel: Record<string, unknown>,
	where: string,
	errors: string[],
): DraftRelation {
	checkKeys(rel, KNOWN_RELATION, where, errors);
	return {
		name: requireName(rel.name, where, errors),
		from: asList(rel.from, `${where}.from`, errors).map(String),
		to: asList(rel.to, `${where}.to`, errors).map(String),
		directed: rel.directed === undefined ? true : asBool(rel.directed, `${where}.directed`, errors),
		required: asBool(rel.required, `${where}.required`, errors),
	};
}

// ── the strict little narrows every parser shares ──

function checkKeys(
	obj: Record<string, unknown>,
	known: Set<string>,
	where: string,
	errors: string[],
): void {
	for (const k of Object.keys(obj)) {
		if (!known.has(k))
			errors.push(`${where}: unknown key \`${k}\` — expected ${[...known].join(', ')}`);
	}
}

function asMapping(
	value: unknown,
	where: string,
	errors: string[],
): Record<string, unknown> | null {
	if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
		return value as Record<string, unknown>;
	}
	errors.push(`${where} must be a mapping with a \`name\``);
	return null;
}

function requireName(value: unknown, where: string, errors: string[]): string {
	if (typeof value === 'string') return value;
	errors.push(`${where} needs a string \`name\``);
	return '';
}

function asList(value: unknown, where: string, errors: string[]): unknown[] {
	if (value === undefined || value === null) return [];
	if (Array.isArray(value)) return value;
	errors.push(`\`${where}\` must be a list`);
	return [];
}

function asBool(value: unknown, where: string, errors: string[]): boolean {
	if (value === undefined || value === null) return false;
	if (typeof value === 'boolean') return value;
	errors.push(`\`${where}\` must be true or false`);
	return false;
}
