/**
 * Wire types for the annotation-projects plane, validated with valibot at the BFF boundary.
 *
 * The backend supplies `legal_events` derived from its own transition tables (machines.py) — the
 * UI renders THAT list and never hardcodes a second copy of the state machine. Each legal event
 * carries the permission that gates it so the UI can explain an action; the actual gate stays
 * server-side, where a denial is a 403, not a disabled button.
 */
import * as v from 'valibot';

export const ProjectStateSchema = v.picklist([
	'draft',
	'labeling',
	'frozen',
	'publishing',
	'published',
	'publish_failed',
	'archived',
]);
export type ProjectState = v.InferOutput<typeof ProjectStateSchema>;

export const TaskStateSchema = v.picklist([
	'unassigned',
	'claimed',
	'in_review',
	'changes_requested',
	'accepted',
	'skipped',
]);
export type TaskState = v.InferOutput<typeof TaskStateSchema>;

/** One principal-fireable transition, straight from the machine tables. */
export const LegalEventSchema = v.object({
	event: v.string(),
	to: v.string(),
	permission: v.string(),
});
export type LegalEvent = v.InferOutput<typeof LegalEventSchema>;

const PublishRecordSchema = v.object({
	table_id: v.string(),
	namespace: v.string(),
	version: v.number(),
	tag: v.nullish(v.string()),
	publish_id: v.string(),
	published_at: v.string(),
	published_by: v.string(),
});

/** One typed field an annotation of its owning class must (or may) answer. */
const OutputAttrSchema = v.object({
	name: v.string(),
	type: v.optional(v.string(), 'free'),
	choices: v.optional(v.array(v.string()), []),
	required: v.optional(v.boolean(), false),
});

/** One class in the taxonomy — and the ONLY place its tools and attributes are declared. */
const LabelClassSchema = v.object({
	name: v.string(),
	colour: v.nullish(v.string()),
	/** Which primitives this class may be drawn with. Empty = any. */
	tools: v.optional(v.array(v.string()), []),
	attributes: v.optional(v.array(OutputAttrSchema), []),
	/** At-least-once. EXPLICIT per class — it used to be a project-level `required_labels` list the
	 *  create dialog filled from "every class name", which silently made every class mandatory on
	 *  every item the moment a third was added. */
	required: v.optional(v.boolean(), false),
});

/** A typed LINK between two annotations — the structure KIE and DocVQA need. */
const RelationClassSchema = v.object({
	name: v.string(),
	from_classes: v.optional(v.array(v.string()), []),
	to_classes: v.optional(v.array(v.string()), []),
	directed: v.optional(v.boolean(), true),
	required: v.optional(v.boolean(), false),
});

/**
 * The whole labeling task definition, as ONE object — mirrors the service's `LabelOntology`.
 *
 * Replaces the `TaskTemplateSchema` + `label_schema` PAIR. Nothing cross-checked those two, and
 * they disagreed in ways the estate acted on: the create dialog derived a `required_labels` list
 * from every class name AND a separate taxonomy from the same textarea, the published facet stamped
 * its class list from one while enforcement read the other, and the class list was never captured
 * onto a task at all — so the one property a taxonomy is FOR, that a label belongs to it, went
 * unenforced and `label="asdf"` published.
 *
 * There is no `enforce` flag. It was a document-wide kill switch that voided explicitly authored
 * declarations; here an ontology with no classes constrains nothing, and everything declared
 * applies. `kind` is a free string (aligned with Hugging Face pipeline ids by convention), because
 * a task-type name buys a shared vocabulary, not a contract.
 */
const LabelOntologySchema = v.object({
	kind: v.optional(v.string(), ''),
	modality: v.optional(v.string(), 'image'),
	classes: v.optional(v.array(LabelClassSchema), []),
	relations: v.optional(v.array(RelationClassSchema), []),
	allow_empty: v.optional(v.boolean(), false),
});
type LabelOntology = v.InferOutput<typeof LabelOntologySchema>;

/** True when this ontology says anything at all — the client mirror of `LabelOntology.constrains`.
 *  Module-local: `ontologyTools` is the only thing that needs it, and the only thing exported. */
function constrains(ontology: LabelOntology | undefined): boolean {
	return Boolean(ontology && (ontology.classes.length > 0 || ontology.relations.length > 0));
}

/**
 * The union of every class's tools — DERIVED, so it cannot disagree with the taxonomy.
 *
 * Empty means unconstrained, and one unconstrained class makes the whole task unconstrained: if a
 * class may be drawn with anything, restricting the rail would hide a tool that class permits.
 * Mirrors `LabelOntology.tools` server-side; the two must agree, which is why both are derived from
 * the same field rather than declared beside it.
 */
export function ontologyTools(ontology: LabelOntology | undefined): string[] {
	if (!constrains(ontology) || !ontology) return [];
	if (ontology.classes.some((c) => c.tools.length === 0)) return [];
	return [...new Set(ontology.classes.flatMap((c) => c.tools))];
}

/** Consensus v1's merge step: the manager's canonical PICK for one replica group. */
export const AdjudicationSchema = v.object({
	task_id: v.string(),
	by: v.string(),
	at: v.string(),
});
export type Adjudication = v.InferOutput<typeof AdjudicationSchema>;

export const ProjectSchema = v.object({
	project_id: v.string(),
	tenant: v.string(),
	slug: v.string(),
	title: v.optional(v.string(), ''),
	description: v.optional(v.string(), ''),
	// Annotator-facing labeling instructions (how to label), distinct from description (what/why).
	instructions: v.optional(v.string(), ''),
	state: ProjectStateSchema,
	review_required: v.optional(v.boolean(), true),
	lease_seconds: v.optional(v.number(), 1800),
	// Consensus v1: how many independent annotators label each sent item (1 = ordinary).
	consensus_n: v.optional(v.number(), 1),
	// Replica group id → the manager's canonical pick (empty until someone adjudicates).
	adjudications: v.optional(v.record(v.string(), AdjudicationSchema), {}),
	// The whole task definition; an empty one is the unconstrained default.
	ontology: v.optional(LabelOntologySchema, {
		kind: '',
		modality: 'image',
		classes: [],
		relations: [],
		allow_empty: false,
	}),
	counts: v.optional(v.record(v.string(), v.number()), {}),
	created_at: v.nullish(v.string()),
	created_by: v.nullish(v.string()),
	updated_at: v.nullish(v.string()),
	published: v.nullish(PublishRecordSchema),
	publish_error: v.nullish(v.string()),
	publish_progress: v.nullish(v.string()),
	pending_target_namespace: v.nullish(v.string()),
});
export type Project = v.InferOutput<typeof ProjectSchema>;

export const ProjectListSchema = v.object({
	projects: v.array(ProjectSchema),
	total: v.number(),
});
export type ProjectList = v.InferOutput<typeof ProjectListSchema>;

export const ProjectDetailSchema = v.object({
	project: ProjectSchema,
	legal_events: v.array(LegalEventSchema),
});
export type ProjectDetail = v.InferOutput<typeof ProjectDetailSchema>;

const ReviewNoteSchema = v.object({
	by: v.string(),
	at: v.string(),
	action: v.string(),
	message: v.optional(v.string(), ''),
	shape_ids: v.optional(v.array(v.string()), []),
});

/** The full task document (the details fan-out), plus its own legal events. */
export const TaskDetailSchema = v.object({
	task_id: v.string(),
	project_id: v.string(),
	state: TaskStateSchema,
	assignee: v.nullish(v.string()),
	lease_expires_at: v.nullish(v.string()),
	source: v.object({
		kind: v.string(),
		keys: v.optional(v.array(v.string()), []),
		where: v.nullish(v.string()),
	}),
	media: v.object({
		kind: v.string(),
		image_url: v.nullish(v.string()),
		media_url: v.nullish(v.string()),
	}),
	review_required: v.optional(v.boolean(), true),
	// Consensus v1: the replica group this item belongs to; null for an ordinary item.
	replica_of: v.nullish(v.string()),
	submitted_by: v.nullish(v.string()),
	submitted_at: v.nullish(v.string()),
	reviewed_by: v.nullish(v.string()),
	reviewed_at: v.nullish(v.string()),
	review_action: v.nullish(v.string()),
	review_notes: v.optional(v.array(ReviewNoteSchema), []),
	legal_events: v.optional(v.array(LegalEventSchema), []),
	// The ontology CAPTURED onto this item at send. The server has always sent the capture —
	// valibot's `v.object` strips unknown keys, so declaring it here is what lets the canvas see the
	// contract it will be judged against, instead of learning the rule from a 409 after the work.
	ontology: v.optional(LabelOntologySchema),
});
export type TaskDetail = v.InferOutput<typeof TaskDetailSchema>;

export const TaskListingSchema = v.object({
	tasks: v.record(v.string(), v.string()),
	counts: v.record(v.string(), v.number()),
	total: v.number(),
	terminal: v.number(),
	may_publish: v.boolean(),
	details: v.optional(v.array(TaskDetailSchema)),
	missing: v.optional(v.array(v.string())),
});
export type TaskListing = v.InferOutput<typeof TaskListingSchema>;

/** One item sent into a labeling task: where it comes from (chunk keys / a scope) and how it is
 *  displayed. The send command validates the same shape at the wire boundary. */
export type SendItem = {
	source: { kind: string; keys: string[]; where?: string | null };
	media: { kind: string; image_url?: string | null; media_url?: string | null };
};

export const DraftSchema = v.object({
	task_id: v.string(),
	project_id: v.string(),
	author: v.string(),
	shapes: v.array(v.record(v.string(), v.unknown())),
	revision: v.number(),
	origin: v.optional(v.string(), 'human'),
});
export type Draft = v.InferOutput<typeof DraftSchema>;
