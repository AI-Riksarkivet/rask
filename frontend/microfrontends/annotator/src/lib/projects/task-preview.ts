/**
 * The DESIGNER's live preview model — what workspace the current draft would build.
 *
 * A pure projection of `TaskDraft`, mirroring how the real workspace derives its surfaces from
 * the ontology: the drawing rail from the union of draw tools, quick-label chips from the
 * spatial classes, the classification bar from `tag` classes, the span surface (digit hotkeys)
 * from `span` classes, the inspector's transcription/typed fields from per-class declarations,
 * and the link rail from surviving relations. Pure and separately tested so the preview cannot
 * quietly diverge from the draft it claims to show.
 */

import type { DraftField, TaskDraft } from './task-yaml.js';

export interface PreviewInspector {
	class: string;
	transcribe: boolean;
	fields: DraftField[];
}

export interface TaskPreviewModel {
	/** The drawing rail: union of every label's draw tools, in first-seen order. */
	draw: string[];
	/** Quick-label chips — the spatial classes (something to draw, then label). */
	labels: { name: string; required: boolean }[];
	/** The classification chip bar — whole-item choices. */
	tags: string[];
	/** The span surface's classes, hotkey-numbered 1–9 like the real keymap. */
	spans: string[];
	/** Inspector rows for classes declaring transcription and/or typed fields. */
	inspectors: PreviewInspector[];
	/** Relations whose endpoints still exist (the same ghost-edge rule as the payload). */
	relations: { name: string; from: string[]; to: string[] }[];
	/** True when nothing is declared — the workspace would be unconstrained. */
	empty: boolean;
}

export function previewModel(draft: TaskDraft): TaskPreviewModel {
	const named = draft.classes.filter((c) => c.name.trim() !== '');
	const draw = [...new Set(named.flatMap((c) => c.draw))];
	const labels = named
		.filter((c) => c.draw.length > 0)
		.map((c) => ({ name: c.name.trim(), required: c.required }));
	const tags = named.filter((c) => c.tag).map((c) => c.name.trim());
	const spans = named.filter((c) => c.span).map((c) => c.name.trim());
	const inspectors = named
		.filter((c) => c.transcribe || c.fields.some((f) => f.name.trim() !== ''))
		.map((c) => ({
			class: c.name.trim(),
			transcribe: c.transcribe,
			fields: c.fields.filter((f) => f.name.trim() !== ''),
		}));
	const names = new Set(named.map((c) => c.name.trim()));
	const relations = draft.relations
		.filter((r) => [...r.from, ...r.to].every((n) => names.has(n)))
		.map((r) => ({ name: r.name, from: r.from, to: r.to }));
	return {
		draw,
		labels,
		tags,
		spans,
		inspectors,
		relations,
		empty: named.length === 0,
	};
}
