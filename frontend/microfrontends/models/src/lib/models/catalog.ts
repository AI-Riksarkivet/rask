// The model registry's wire shapes. The catalog serves no model registry (LH-558), so nothing generates
// them and they are declared here; the doors this zone calls answer 404. A null field may be absent rather
// than null, so read optional fields with `?? null`.
//
// The registry TRANSPORT (list / describe / promote) lives in `remote/models.remote.ts` — the zone's
// remote-function dialect. A remote file may export only remote functions, so the types stay here.

export type ModelSummary = {
	model: string;
	latest_version: number | null;
	blessed_version: number | null;
};

export type ModelsList = {
	models: ModelSummary[];
	page_token?: string | null;
};

/** One object under the model's artifact tree (path relative to the model root) — the describe
 * response's `artifacts` listing the registry detail renders as a table. */
export type ModelArtifact = {
	path: string;
	size_bytes: number;
	updated_at: string | null;
};

export type ModelDescribe = {
	model: string;
	latest_version: number;
	blessed_version: number | null;
	candidate_metrics: { [key: string]: unknown } | null;
	blessed_metrics: { [key: string]: unknown } | null;
	artifacts?: ModelArtifact[];
};

export type PromoteResponse = {
	model: string;
	blessed_version: number;
	tag: string;
};
