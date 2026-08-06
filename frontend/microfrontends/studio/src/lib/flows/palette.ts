/**
 * The node library's vocabulary — what you can drop on the canvas, grouped the way the PLATFORM
 * thinks rather than the way the editor's type system does.
 *
 * Deliberately **store-free and Svelte-free**: the library renders in the zone's SIDEBAR, which is
 * mounted by the shared root layout, and the layout must not pull in `graph.svelte.ts` — that module
 * is a singleton whose constructor reads localStorage, so importing it from the layout would
 * instantiate the graph on every studio route — and on the SERVER, where the canvas page is
 * deliberately `ssr = false`. So this module carries only data, and the rail talks to the canvas
 * through a DataTransfer payload or a window event — never through the store.
 *
 * A dropped entry may carry a CONFIG PRESET, which is what makes a live Ray Serve deployment a
 * first-class palette item: dragging `gemma-31b` drops a `model` node already pointed at it, instead
 * of a blank node the user then has to configure.
 */
import type { NodeConfig, NodeKind } from './types';

/** MIME carrying the drop payload across the HTML5 drag (rail → canvas). */
export const NODE_DND_MIME = 'application/studio-flow-node';

/** Window event the rail dispatches when an entry is CLICKED rather than dragged — the canvas is
 *  the only place that knows where the viewport centre is, so it decides the position. */
export const ADD_NODE_EVENT = 'studio-flow:add-node';

/** What a drop asks for: a kind, and optionally the config it should land pre-filled with. */
export interface PaletteDrop {
	kind: NodeKind;
	config?: Partial<NodeConfig>;
}

/** One row of the library. */
export interface PaletteEntry extends PaletteDrop {
	label: string;
	blurb: string;
	/** Set for a row that stands for a live cluster resource, so the UI can mark it as such. */
	live?: boolean;
}

export interface PaletteGroup {
	label: string;
	entries: PaletteEntry[];
	/** Shown in place of the rows when the group is empty (discovery offline, nothing deployed). */
	emptyNote?: string;
}

/** The kinds the editor ships, grouped by the job they do in a pipeline. `model` sits under
 *  INFERENCE as the generic "call some endpoint" node; the Ray Serve group below is the same kind
 *  with the app already chosen. */
export const STATIC_GROUPS: PaletteGroup[] = [
	{
		label: 'Inputs',
		entries: [
			{ kind: 'image', label: 'Image', blurb: 'Upload a page scan — emits its bytes.' },
			{ kind: 'text', label: 'Text', blurb: 'Type a payload to send downstream.' },
			{
				kind: 'dataset',
				label: 'Lakehouse table',
				blurb: 'Governed rows — SCAFFOLD, reads nothing yet.',
			},
		],
	},
	{
		label: 'Inference',
		entries: [
			{
				kind: 'model',
				label: 'Model endpoint',
				blurb: 'POST the upstream payload to any Serve app.',
			},
		],
	},
	{
		label: 'Tools',
		entries: [
			{
				kind: 'mcp',
				label: 'MCP tool',
				blurb: 'Call an MCP tool — SCAFFOLD, no client yet.',
			},
		],
	},
	{
		label: 'Transforms',
		entries: [
			{ kind: 'prompt', label: 'Prompt', blurb: 'Compose the text a model will be asked.' },
			{
				kind: 'extract',
				label: 'Extract JSON',
				blurb: 'Pull a field out by path — makes an answer chainable.',
			},
			{ kind: 'regex', label: 'Regex', blurb: 'Extract or replace by pattern.' },
			{ kind: 'alto', label: 'ALTO → text', blurb: 'Pull the transcription out of ALTO XML.' },
		],
	},
	{
		label: 'Outputs',
		entries: [
			{ kind: 'compare', label: 'Compare', blurb: 'A/B two branches side by side.' },
			{ kind: 'inspect', label: 'Inspect', blurb: 'Render whatever reaches this node.' },
		],
	},
];

/** Build the RAY SERVE group from live discovery: one draggable row per deployment, each dropping a
 *  `model` node already pointed at that app. This is the group that makes the library about the
 *  cluster rather than about the editor. */
export function serveGroup(
	apps: { name: string; app: string; status: string }[] | null,
	detail?: string,
): PaletteGroup {
	if (apps === null) {
		return {
			label: 'Ray Serve',
			entries: [],
			emptyNote: detail ? `Discovery offline — ${detail}` : 'Looking for live Serve apps…',
		};
	}
	return {
		label: 'Ray Serve',
		emptyNote: 'No Serve apps are deployed — try `make serve-up-htrflow`.',
		entries: apps.map((a) => ({
			kind: 'model' as const,
			label: a.name,
			blurb: `/${a.app} · ${a.status}`,
			config: { app: a.app },
			live: true,
		})),
	};
}

/** Case-insensitive filter over a group's rows, matching label and blurb — so typing "alto" finds
 *  the transform and typing "running" finds every healthy deployment. */
export function filterGroups(groups: PaletteGroup[], query: string): PaletteGroup[] {
	const q = query.trim().toLowerCase();
	if (!q) return groups;
	return (
		groups
			.map((g) => ({
				...g,
				entries: g.entries.filter(
					(e) => e.label.toLowerCase().includes(q) || e.blurb.toLowerCase().includes(q),
				),
			}))
			// A group with no match disappears entirely — including its empty note, which would
			// otherwise read as "no results" for a group the query simply does not concern.
			.filter((g) => g.entries.length > 0)
	);
}

/** Serialise a drop for the DataTransfer. JSON rather than a bare kind string because an entry may
 *  carry a config preset; `decodeDrop` still accepts the bare form. */
export function encodeDrop(drop: PaletteDrop): string {
	return JSON.stringify(drop);
}

/** Parse a DataTransfer payload back into a drop. Returns null for anything unrecognised — the
 *  canvas treats that as "not our drag" and ignores it (a file dragged onto the pane, say).
 *  `isKind` is passed in so this module stays free of the store's re-exports. */
export function decodeDrop(
	raw: string | undefined,
	isKind: (v: string) => boolean,
): PaletteDrop | null {
	if (!raw) return null;
	// Bare kind (an older drag, or a hand-built payload).
	if (isKind(raw)) return { kind: raw as NodeKind };
	try {
		const parsed: unknown = JSON.parse(raw);
		if (typeof parsed !== 'object' || parsed === null) return null;
		const { kind, config } = parsed as { kind?: unknown; config?: unknown };
		if (typeof kind !== 'string' || !isKind(kind)) return null;
		return {
			kind: kind as NodeKind,
			...(config && typeof config === 'object' ? { config: config as Partial<NodeConfig> } : {}),
		};
	} catch {
		return null;
	}
}
