/**
 * atlas-legend — PURE legend math for AtlasMap (no Svelte, no DOM, no store).
 *
 * The legends summarise WHAT'S IN VIEW: counts + distribution bars are tallied
 * over the `visible` index set (the intersection of the search-filter and the
 * map-selection; null ⇒ nothing active ⇒ every point). Swatch colours read the
 * SAME `hueCss` hues the WebGPU scatter uses, so a swatch can never disagree
 * with its points. Every function here is single-responsibility + unit-testable.
 */
import { hueCss } from './atlas-colors';
import type { ColorBy } from './cross-filter.svelte';

export type ClusterLegendRow = { id: number; color: string; count: number; frac: number };
export type CategoryLegendRow = {
	code: number;
	label: string;
	color: string;
	count: number;
	frac: number;
	empty: boolean;
};
// `codes` is `ArrayLike<number>` (not `readonly number[]`) so an `Int32Array`
// from the points payload satisfies it — consumers only index + read `.length`.
export type CategoryChannel = { codes: ArrayLike<number>; labels: readonly string[] };
export type ClusterRanking = { slotOf: Map<number, number>; distinct: number };

/** Intersection of the search-filter set and the map-selection set (null ⇒ all points). */
export function visibleIds(
	filteredIds: Set<number> | null,
	selectedIds: Set<number> | null,
): Set<number> | null {
	const f = filteredIds;
	const s = selectedIds;
	if (f && s) {
		const [small, big] = f.size <= s.size ? [f, s] : [s, f];
		const both = new Set<number>();
		for (const i of small) if (big.has(i)) both.add(i);
		return both;
	}
	return f ?? s ?? null;
}

/** Tally code[i] over the visible indices (or every point when null). `codes`
 *  is `ArrayLike<number>` (an `Int32Array` from the points payload), so the
 *  no-`visible` path uses an index loop, not `for…of` (a TypedArray iterates
 *  fine, but `ArrayLike` carries no iterator in the type). */
function tally(codes: ArrayLike<number>, visible: Set<number> | null): Map<number, number> {
	const counts = new Map<number, number>();
	if (visible) {
		for (const i of visible) {
			const code = codes[i];
			if (code !== undefined) counts.set(code, (counts.get(code) ?? 0) + 1);
		}
	} else {
		for (let i = 0; i < codes.length; i++) {
			const code = codes[i]!;
			counts.set(code, (counts.get(code) ?? 0) + 1);
		}
	}
	return counts;
}

/** Largest value (≥1) — normalises the distribution bars. */
function maxCount(values: Iterable<number>): number {
	let max = 1;
	for (const v of values) if (v > max) max = v;
	return max;
}

/** Legend rows for cluster mode (clickable to select a cluster). Caller guards
 *  that the points have a `cluster` column and `ranking` is resolved. */
export function buildClusterLegend(
	cluster: ArrayLike<number>,
	ranking: ClusterRanking,
	visible: Set<number> | null,
	isDark: boolean,
): ClusterLegendRow[] {
	const { slotOf, distinct } = ranking;
	const rows = [...tally(cluster, visible).entries()].filter(([id]) => id >= 0);
	const max = maxCount(rows.map(([, count]) => count));
	return rows
		.map(([id, count]) => {
			const rank = slotOf.get(id) ?? -1;
			const color =
				rank < 0 ? (isDark ? '#71717a' : '#a1a1aa') : hueCss(rank + 1, distinct + 1, isDark);
			return { id, color, count, frac: count / max };
		})
		.sort((a, b) => b.count - a.count);
}

/** Distinct non-noise cluster count + noise (unclustered) point count — for
 *  the legend's "showing X of Y" line and the noise summary row. */
export function clusterStats(
	cluster: ArrayLike<number> | undefined,
	visible: Set<number> | null,
): { total: number; noise: number } {
	if (!cluster) return { total: 0, noise: 0 };
	let total = 0;
	let noise = 0;
	for (const [id, count] of tally(cluster, visible)) {
		if (id < 0) noise += count;
		else total++;
	}
	return { total, noise };
}

/** Legend rows for the active categorical colour mode (language / topic /
 *  doc_topic) — same hues the map uses, clickable to select that category. */
export function buildCategoryLegend(
	channel: CategoryChannel | null,
	visible: Set<number> | null,
	isDark: boolean,
	maxDistinct: number,
): CategoryLegendRow[] {
	if (!channel) return [];
	const { codes, labels } = channel;
	const distinct = Math.min(maxDistinct, labels.length);
	const grey = isDark ? '#71717a' : '#a1a1aa';
	const noiseCss = isDark ? '#52525b' : '#cccccc';
	const counts = tally(codes, visible);
	const max = maxCount(counts.values());
	return [...counts.entries()]
		.map(([code, count]) => {
			const raw = labels[code] ?? '';
			const empty = raw === '';
			return {
				code,
				count,
				empty,
				frac: count / max,
				label: empty ? '(ej klustrad)' : raw,
				color: empty ? noiseCss : code < distinct ? hueCss(code + 1, distinct + 1, isDark) : grey,
			};
		})
		.sort((a, b) => b.count - a.count);
}

/** Humanize a descriptor field/channel/space name for a menu or legend label
 *  (`doc_topic` → `Doc Topic`) — derived from the descriptor string at runtime,
 *  never a hardcoded corpus column. */
export function channelLabel(name: string): string {
	return name
		.split(/[_\s]+/)
		.filter((w) => w.length > 0)
		.map((w) => w.charAt(0).toUpperCase() + w.slice(1))
		.join(' ');
}

/** The legend panel's title for the active categorical colour mode — the
 *  humanized channel name (empty for the special `cluster`/`none` modes). */
export function categoryTitle(colorBy: ColorBy): string {
	return colorBy === 'cluster' || colorBy === 'none' ? '' : channelLabel(colorBy);
}
