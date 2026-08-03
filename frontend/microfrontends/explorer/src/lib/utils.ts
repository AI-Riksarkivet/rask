import { activeView, type Row } from '@rask/media-api/descriptor';

/** shadcn-svelte's standard `cn` helper: clsx for conditionals, twMerge for conflicts. */
export { cn } from '@rask/ui/utils';
export type {
	WithElementRef,
	WithoutChild,
	WithoutChildren,
	WithoutChildrenOrChild,
} from '@rask/ui/utils';

// Type helpers expected by shadcn-svelte v1.2+ generated components (shared
// with the sibling apps so UI primitives stay copy-paste compatible).

/** Format seconds as `H:MM:SS` (or `M:SS` under an hour). */
export function fmtTime(s: number): string {
	const total = Math.round(s);
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const sec = total % 60;
	const pad = (n: number) => String(n).padStart(2, '0');
	return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

/** Escape HTML special characters for safe innerHTML interpolation. */
function escapeHtml(s: string | null | undefined): string {
	return String(s ?? '').replace(
		/[&<>]/g,
		(c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c] ?? c,
	);
}

/** Lowercased content words from a Tantivy-style query. */
export function queryTerms(q: string): string[] {
	const stop = new Set(['and', 'or', 'not', 'near']);
	// `\w` excludes Unicode letters (ö/å/ä) even with /u — must use \p{L}.
	return (q.match(/[\p{L}\p{N}_]+/gu) ?? [])
		.map((t) => t.toLowerCase())
		.filter((t) => !stop.has(t));
}

/** Stable identity key for a row, composed from the dataset descriptor's
 *  identity key fields (replaces the old hardcoded doc/speech/chunk shape). */
export const hitKey = (h: Row): string => activeView().rowKey(h);

/**
 * Build a highlighter that wraps `terms` in <mark>…</mark> inside HTML-escaped
 * text. The match regex is compiled once here, so the returned function is cheap
 * to call per row/cell; its output is safe to inject with `{@html}`.
 */
export function makeHighlighter(terms: string[]): (text: string) => string {
	if (terms.length === 0) return escapeHtml;
	const pattern = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
	const re = new RegExp(`(${pattern})`, 'giu');
	return (text) =>
		escapeHtml(text).replace(
			re,
			'<mark class="bg-highlight/30 text-foreground rounded-sm">$1</mark>',
		);
}
