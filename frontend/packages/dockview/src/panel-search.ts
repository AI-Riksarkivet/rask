import type { PanelEntry, PanelRegistry } from './types';

/** A registry entry flattened for the picker, with its registry key kept alongside. */
export interface PanelChoice extends PanelEntry {
	/** The registry key — what `addPanel({ component })` takes, and what a layout resolves by. */
	readonly key: string;
	/** True when a panel of this component is already open somewhere in the dock. */
	readonly open: boolean;
}

/**
 * Match a query against a panel's label, registry key and keywords.
 *
 * Deliberately tiny and deliberately not fuzzy. No fuse.js, no cmdk — the whole picker has to fit in
 * a deferred budget with single-digit KB of headroom, and a catalogue of three-to-nine entries does
 * not need ranked subsequence matching. Substring over the joined haystack is what a user of a
 * nine-item list actually expects: typing "top" finds "Topic results", and typing "pic" does too.
 *
 * Case-insensitive, and whitespace-separated terms are ANDed so "topic res" still matches — that is
 * the one thing plain `includes()` gets wrong often enough to be worth the four extra lines.
 */
export function matchesPanel(entry: PanelChoice, query: string): boolean {
	const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
	if (terms.length === 0) return true;
	const haystack = [entry.label, entry.key, ...(entry.keywords ?? [])].join(' ').toLowerCase();
	return terms.every((term) => haystack.includes(term));
}

/**
 * The catalogue as the picker renders it: registry order, each entry told whether it is already open.
 *
 * Already-open panels are LISTED, not hidden and not disabled. Hiding them makes the list jump as the
 * user works and hides the vocabulary of the workbench; disabling them forbids something legitimate —
 * two Runs panels side by side, filtered differently, is a real thing to want, and `uniquePanelId`
 * exists precisely so a second copy is safe. They carry `open` so the row can say so quietly.
 */
export function panelChoices(
	panels: PanelRegistry,
	openComponents: readonly string[],
): PanelChoice[] {
	const open = new Set(openComponents);
	return Object.entries(panels).map(([key, entry]) => ({ ...entry, key, open: open.has(key) }));
}
