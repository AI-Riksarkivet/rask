/**
 * Persisted column preferences for the search page's tables — load/save
 * helpers kept out of +page.svelte so the merge semantics live (and read) in
 * one place.
 *
 * Three flavours:
 *   • prefs ({cols, known, widths, wrap}) — the merged format plus per-column
 *     dragged widths (px) and the wrap-text toggle, all in ONE record so a
 *     single key versions every table preference together (v6 era).
 *   • merged ({cols, known}) — for column sets that grow over app versions.
 *     `known` records which columns existed when the user last toggled, so a
 *     column ADDED to the app later starts visible (when it's in the default
 *     set) instead of being hidden forever by a verbatim restore. The old
 *     plain-array format needed a storage-key bump per added column
 *     (v2→v3 caption, v3→v4 score) precisely because it lacked this.
 *   • plain — a verbatim string array for fixed column sets.
 *
 * All storage access is best-effort (private mode / quota → fall back to
 * defaults, never throw).
 */
import * as v from 'valibot';

const SavedColsSchema = v.object({ cols: v.array(v.string()), known: v.array(v.string()) });

const SavedTablePrefsSchema = v.object({
	cols: v.array(v.string()),
	known: v.array(v.string()),
	widths: v.record(v.string(), v.number()),
	wrap: v.boolean(),
});

const StringArraySchema = v.array(v.string());

/** Per-table user prefs: visible columns, dragged column widths (px, by
 *  column key — absent key = auto-sized), and the wrap-text toggle. */
export type TablePrefs = {
	cols: string[];
	widths: Record<string, number>;
	wrap: boolean;
};

export function loadTablePrefs(args: {
	storageKey: string;
	/** Every column key the app currently defines. */
	allKeys: string[];
	/** Columns visible for a fresh user — also what NEW columns merge from. */
	defaults: string[];
	/** One-shot migration source: the previous merged-format ({cols, known}) key. */
	legacyMergedKey?: string | undefined;
	/** The merged era's own plain-array legacy key (chained through). */
	legacyPlainKey?: string | undefined;
	/** Keys to append during plain-legacy migration (see loadMergedCols). */
	legacyAppend?: string[] | undefined;
}): TablePrefs {
	const { storageKey, allKeys, defaults, legacyMergedKey, legacyPlainKey, legacyAppend } = args;
	try {
		const raw = localStorage.getItem(storageKey);
		if (raw) {
			const saved = v.parse(SavedTablePrefsSchema, JSON.parse(raw));
			const fresh = allKeys.filter((k) => !saved.known.includes(k) && defaults.includes(k));
			return { cols: [...saved.cols, ...fresh], widths: saved.widths, wrap: saved.wrap };
		}
		// One-shot migration from the merged-cols era (which itself chains to its
		// plain-array legacy): column visibility carries over; widths/wrap are
		// new in this era, so they start at their defaults. Stamp the upgraded
		// record NOW — same reasoning as the legacy stamp in loadMergedCols.
		const hasLegacy =
			(legacyMergedKey && localStorage.getItem(legacyMergedKey) !== null) ||
			(legacyPlainKey && localStorage.getItem(legacyPlainKey) !== null);
		if (hasLegacy && legacyMergedKey) {
			const cols = loadMergedCols({
				storageKey: legacyMergedKey,
				allKeys,
				defaults,
				legacyKey: legacyPlainKey,
				legacyAppend,
			});
			const prefs: TablePrefs = { cols, widths: {}, wrap: false };
			persistTablePrefs(storageKey, prefs, allKeys);
			return prefs;
		}
	} catch {
		/* fall through to defaults */
	}
	return { cols: [...defaults], widths: {}, wrap: false };
}

export function persistTablePrefs(storageKey: string, prefs: TablePrefs, allKeys: string[]): void {
	try {
		localStorage.setItem(
			storageKey,
			JSON.stringify({ cols: prefs.cols, known: allKeys, widths: prefs.widths, wrap: prefs.wrap }),
		);
	} catch {
		/* best-effort */
	}
}

/** Internal since the v6 prefs era: only the loadTablePrefs migration chain
 *  reads this format now. */
function loadMergedCols(args: {
	storageKey: string;
	/** Every column key the app currently defines. */
	allKeys: string[];
	/** Columns visible for a fresh user — also what NEW columns merge from. */
	defaults: string[];
	/** Optional one-shot migration source: a legacy plain-array key. */
	legacyKey?: string | undefined;
	/** Keys to append during legacy migration (columns newer than the legacy era). */
	legacyAppend?: string[] | undefined;
}): string[] {
	const { storageKey, allKeys, defaults, legacyKey, legacyAppend = [] } = args;
	try {
		const raw = localStorage.getItem(storageKey);
		if (raw) {
			const saved = v.parse(SavedColsSchema, JSON.parse(raw));
			const fresh = allKeys.filter((k) => !saved.known.includes(k) && defaults.includes(k));
			return [...saved.cols, ...fresh];
		}
		if (legacyKey) {
			const legacy = localStorage.getItem(legacyKey);
			if (legacy) {
				const cols = v.parse(StringArraySchema, JSON.parse(legacy));
				const migrated = [...cols, ...legacyAppend.filter((k) => !cols.includes(k))];
				// Stamp the merged record NOW: without a `known` baseline a user who
				// never toggles again would re-migrate forever and miss every column
				// added after the legacy era — the exact failure this format fixes.
				persistMergedCols(storageKey, migrated, allKeys);
				return migrated;
			}
		}
	} catch {
		/* fall through to defaults */
	}
	return [...defaults];
}

function persistMergedCols(storageKey: string, cols: string[], allKeys: string[]): void {
	try {
		localStorage.setItem(storageKey, JSON.stringify({ cols, known: allKeys }));
	} catch {
		/* best-effort */
	}
}

export function loadCols(storageKey: string, defaults: string[]): string[] {
	try {
		const raw = localStorage.getItem(storageKey);
		if (raw) return v.parse(StringArraySchema, JSON.parse(raw));
	} catch {
		/* fall through to defaults */
	}
	return [...defaults];
}

export function persistCols(storageKey: string, cols: string[]): void {
	try {
		localStorage.setItem(storageKey, JSON.stringify(cols));
	} catch {
		/* best-effort */
	}
}
