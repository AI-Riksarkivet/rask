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

export const LabelClassSchema = v.object({
	name: v.string(),
	colour: v.nullish(v.string()),
	shape_types: v.optional(v.array(v.string()), []),
});
export type LabelClass = v.InferOutput<typeof LabelClassSchema>;

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
	label_schema: v.optional(
		v.object({
			classes: v.optional(v.array(LabelClassSchema), []),
			attributes: v.optional(v.array(v.string()), []),
		}),
		{ classes: [], attributes: [] },
	),
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

export const DraftSchema = v.object({
	task_id: v.string(),
	project_id: v.string(),
	author: v.string(),
	shapes: v.array(v.record(v.string(), v.unknown())),
	revision: v.number(),
	origin: v.optional(v.string(), 'human'),
});
export type Draft = v.InferOutput<typeof DraftSchema>;
