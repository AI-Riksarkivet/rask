/**
 * Shared bidirectional cross-filter state for the atlas ⇄ search workspace.
 *
 * One singleton store, two independent Sets over the SAME point universe (the
 * loaded atlas points), keyed by point INDEX (not the descriptor-identity
 * string — index Sets avoid stringify churn on the hot recolour path):
 *
 *   • `filteredIds` — the SEARCH + facet result set projected to point indices
 *     (`null` ⇒ no active search; the whole corpus is in scope).
 *   • `selectedIds` — what the user picked ON THE MAP (lasso / box / cluster /
 *     legend / click).
 *
 * The map recolour reads BOTH Sets per point: a search miss (`filteredIds`)
 * ghosts a point hardest, a selection miss (`selectedIds`) dims it less — so a
 * lasso AFTER a search shows the hits inside the lasso, and a search AFTER a
 * lasso re-highlights within the selection — bidirectional lock-step.
 *
 * Renderer-agnostic: holds plain Sets + scalars, no EmbeddingView types. The
 * map component derives its per-point RGBA alpha from the two Sets; the page
 * feeds `setFilteredFromHits` and reads `selectedIds`.
 */

import type { Hit, AtlasPoints, AtlasSpace } from '@rask/media-api';
import { activeView } from '@rask/media-api/descriptor';
import { hitKey } from '$lib/utils';

/** Colour channel for the scatter (legend + per-point recolour). `cluster` and
 *  `none` are special; any other value is a declared atlas-channel name read
 *  from the descriptor (`activeView().atlasChannels(space)`). */
export type ColorBy = 'cluster' | 'none' | (string & {});

/** Build a `descriptor-identity → point index` map once per loaded space. The
 *  key is the joined identity key fields — the doc key resolved through the
 *  point's `doc` dictionary, the remaining key fields read from `points.keys` —
 *  so it matches {@link hitKey}/`rowKey` and a search hit shares its point's key. */
export function buildKeyIndex(pts: AtlasPoints): Map<string, number> {
	const view = activeView();
	const keyFields = view.keyFields;
	const docKeyField = view.docKeyField;
	const map = new Map<string, number>();
	const n = pts.doc.length;
	for (let i = 0; i < n; i++) {
		const parts: string[] = [];
		let ok = true;
		for (const field of keyFields) {
			if (field === docKeyField) {
				const dc = pts.doc[i];
				const d = dc !== undefined ? pts.docs[dc] : undefined;
				if (d === undefined) {
					ok = false;
					break;
				}
				parts.push(d);
			} else {
				parts.push(String(pts.keys[field]?.[i] ?? ''));
			}
		}
		if (ok) map.set(parts.join('|'), i);
	}
	return map;
}

// `hitKey` lives in `$lib/utils` (single source); re-exported here so atlas
// consumers keep importing it alongside `buildKeyIndex`. Its descriptor-identity
// shape must stay in lock-step with `buildKeyIndex` above.
export { hitKey };

/** Shared sentinel for colour modes with nothing hidden. The store always
 *  reassigns the Map (never this Set), so a plain frozen-by-contract empty Set
 *  is safe to hand out — callers only ever read `.size` / `.has`. */
const EMPTY: ReadonlySet<number> = new Set<number>();

class CrossFilter {
	// All collection fields are `$state.raw` — every mutator below REPLACES the
	// collection (never mutates in place), so raw both works and documents the
	// replace-don't-mutate contract.
	/** Point indices the user picked on the map (untruncated — drives dimming). */
	selectedIds = $state.raw<Set<number>>(new Set());
	/** Point indices of the current search+facet results; null ⇒ no active filter. */
	filteredIds = $state.raw<Set<number> | null>(null);
	/** Which projection the map is showing; the page may read it to fetch per space. */
	space = $state<AtlasSpace>('text');
	/** Colour channel for the scatter. */
	colorBy = $state<ColorBy>('cluster');
	/** Per-colour-mode hidden codes (a code only means something within its own
	 *  colour channel). Keyed by ColorBy; recoloured to background on the map. */
	hiddenByMode = $state.raw<Map<ColorBy, Set<number>>>(new Map());

	/** Descriptor-identity → index for the CURRENT space (rebuilt on space swap). */
	keyToIndex = $state.raw<Map<string, number>>(new Map());

	get hasSelection(): boolean {
		return this.selectedIds.size > 0;
	}
	get hasFilter(): boolean {
		return this.filteredIds !== null;
	}
	/** Hidden codes for the CURRENT colour mode (shared EMPTY set if none). */
	get hidden(): ReadonlySet<number> {
		return this.hiddenByMode.get(this.colorBy) ?? EMPTY;
	}

	// ── setters ──────────────────────────────────────────────────────────────

	/** Project search Hit[] to point indices via the current key→index map. */
	setFilteredFromHits(hits: readonly Hit[]): void {
		const map = this.keyToIndex;
		const next = new Set<number>();
		for (const h of hits) {
			const i = map.get(hitKey(h));
			if (i !== undefined) next.add(i);
		}
		this.filteredIds = next;
	}

	/** Clear the search→map filter (back to the whole corpus). */
	clearFilter(): void {
		this.filteredIds = null;
	}

	/** Replace the map selection with an explicit FULL index set (no truncation). */
	setSelectedIndices(idx: readonly number[]): void {
		this.selectedIds = new Set(idx);
	}

	clearSelection(): void {
		if (this.selectedIds.size > 0) this.selectedIds = new Set();
	}

	/** Select every point in a cluster — a first-class lasso-equivalent.
	 *  `clusters` is `ArrayLike<number>` (an `Int32Array` from the points payload). */
	selectCluster(clusterId: number, clusters: ArrayLike<number>): void {
		const next = new Set<number>();
		for (let i = 0; i < clusters.length; i++) {
			if (clusters[i] === clusterId) next.add(i);
		}
		this.selectedIds = next;
	}

	setSpace(s: AtlasSpace): void {
		this.space = s;
	}

	/** Toggle a code's visibility on the map for the CURRENT colour mode
	 *  (reassigns Set + Map so reactivity fires). */
	toggleHidden(code: number): void {
		const mode = this.colorBy;
		const cur = this.hiddenByMode.get(mode) ?? EMPTY;
		const next = new Set(cur);
		if (next.has(code)) next.delete(code);
		else next.add(code);
		const nextMap = new Map(this.hiddenByMode);
		if (next.size > 0) nextMap.set(mode, next);
		else nextMap.delete(mode); // keep the map clean → getter falls back to EMPTY
		this.hiddenByMode = nextMap;
	}

	/** Un-hide every code in the CURRENT colour mode. */
	showAll(): void {
		const mode = this.colorBy;
		if (!this.hiddenByMode.has(mode)) return;
		const nextMap = new Map(this.hiddenByMode);
		nextMap.delete(mode);
		this.hiddenByMode = nextMap;
	}

	/** Reset everything tied to the loaded universe (called on a space swap). */
	resetForSpace(keyToIndex: Map<string, number>): void {
		this.keyToIndex = keyToIndex;
		this.selectedIds = new Set();
		this.filteredIds = null;
		this.hiddenByMode = new Map();
	}
}

/** The singleton, imported by both the page and AtlasMap so they lock-step. */
export const crossFilter = new CrossFilter();
