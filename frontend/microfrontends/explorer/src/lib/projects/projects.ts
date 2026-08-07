/**
 * The annotation-projects contracts the SEND half of the funnel needs — and nothing more.
 *
 * Media is where data points are FOUND (search hits, an atlas lasso) and sent into an annotation
 * project; it never claims, reviews or publishes, so it needs exactly three shapes: the picker's row,
 * the freshly-created project's id, and what a send did. The full project plane lives in the annotator
 * zone, which owns the other end.
 *
 * The wire types stay HERE rather than in `projects.remote.ts` because a remote module may export only
 * remote functions. The three RESPONSE shapes are valibot schemas with derived types (#94): they are
 * hand-mirrored from the annotator service with no codegen, so the wire is parsed against them and a
 * drift is a named failure the dialog renders, never a silently-empty picker.
 */
import * as v from 'valibot';

/** One project as the picker renders it (title/slug/state); `counts` rides along as the server sends it. */
export const ProjectRowSchema = v.looseObject({
	project_id: v.string(),
	slug: v.string(),
	title: v.string(),
	state: v.string(),
	counts: v.record(v.string(), v.number()),
});
export type ProjectRow = v.InferOutput<typeof ProjectRowSchema>;

export const ProjectsListSchema = v.looseObject({ projects: v.array(ProjectRowSchema) });
export type ProjectsList = v.InferOutput<typeof ProjectsListSchema>;

/** A created project. `project_id` is optional on the wire: a 2xx without one is a contract failure the
 *  dialog reports rather than a project it can send to. */
export const CreatedProjectSchema = v.looseObject({
	project_id: v.optional(v.string()),
	slug: v.optional(v.string()),
	title: v.optional(v.string()),
	state: v.optional(v.string()),
});
export type CreatedProject = v.InferOutput<typeof CreatedProjectSchema>;

/** What a send did — `sent` items, `created` of them new (a re-sent item is not duplicated). */
export const SendResultSchema = v.looseObject({
	sent: v.optional(v.number()),
	created: v.optional(v.number()),
});
export type SendResult = v.InferOutput<typeof SendResultSchema>;

/**
 * One item of a send: a descriptor key-path plus the provenance the task carries — which dataset, and
 * its VERSION at send time (§4.5's reproducibility capture, which the publish pin rests on). `null` is
 * an honest "uncaptured"; no pin is ever fabricated.
 */
export interface SendItem {
	source: {
		kind: 'chunks';
		keys: string[];
		where: string | null;
		dataset_version: number | null;
	};
	media: { kind: 'image' };
	/** PRE-ANNOTATIONS attached at send — the bulk-label payload, mirroring the service's
	 *  `PredictionShape`. Absent for an ordinary send.
	 *
	 *  No `source`: the service stamps it (`BULK_SOURCE`), and it is how every later surface tells
	 *  suggested work from drawn work. A sender able to write it could mark items nobody looked at
	 *  as human work. */
	prediction?: { shape_type: string; label: string }[];
}
