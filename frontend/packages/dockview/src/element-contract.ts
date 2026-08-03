import * as v from 'valibot';

/**
 * The CROSS-ZONE panel contract — the only wire between a zone-owned custom element and the
 * global workbench compositor (open_workbench.md). Everything else is forbidden: no shared
 * stores, no imports across the boundary. Data flows DOWN as element properties (rich JS
 * values, set by assignment) and UP as this one bubbling, composed CustomEvent.
 *
 * TypeScript cannot check across a script-tag boundary, so the contract is enforced twice:
 * these types pin both sides at build time (the zone imports them to dispatch, the compositor
 * to relay), and the compositor RUNTIME-VALIDATES every inbound detail with the schema below —
 * a drifted zone degrades to a logged, dropped event, never a corrupted sibling panel.
 */

/** The one event a foreign panel may dispatch. Bubbling + composed, so the compositor can
 *  listen once, by delegation, on the dock container. */
export const RASK_SELECT = 'rask:select';

export const SelectDetailSchema = v.object({
	/** The dispatching element's tag — `rask-<zone>-<panel>`. */
	source: v.string(),
	/** What was selected — a namespaced noun (`ray-job`, `lineage-run`, `media-topic`, …). */
	kind: v.string(),
	id: v.string(),
	/** Human-readable, for surfaces that log or announce the selection. */
	label: v.string(),
});
export type SelectDetail = v.InferOutput<typeof SelectDetailSchema>;

/** Parse an inbound event's detail, or null — the compositor's relay gate. */
export function parseSelectDetail(detail: unknown): SelectDetail | null {
	const r = v.safeParse(SelectDetailSchema, detail);
	return r.success ? r.output : null;
}
