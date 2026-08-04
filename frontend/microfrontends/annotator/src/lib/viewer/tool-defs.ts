/**
 * The tool registry — ONE list driving the toolbar buttons, the shell's hotkey map,
 * and the cv/drawing eligibility flags. Add a tool here (+ its engine class in
 * InteractionManager) and every surface picks it up; previously the keymap lived in
 * three hand-maintained copies that could silently drift.
 */
import {
	Crosshair,
	Hand,
	Lasso,
	Magnet,
	Minus,
	MousePointer2,
	Paintbrush,
	PenLine,
	Pentagon,
	Square,
} from '@lucide/svelte';
import type { Tool } from '@rask/engine';

export interface ToolDef {
	tool: Tool;
	icon: typeof MousePointer2;
	label: string;
	/** Hotkey (single char; letters lowercase in the keymap, shown uppercase). */
	key: string;
	/** A drawing tool (hidden outside edit mode / for non-spatial units). */
	drawing: boolean;
	/** Needs the still-image corner pipeline (unavailable over video frames). */
	cv?: boolean;
	/** Which band of the rail this sits in. Separate from `drawing`, which was doing double duty:
	 *  `lasso` is a DRAWING tool by that flag (it is an edit-mode affordance) while committing no
	 *  shape at all — it selects. Grouping by what a tool is FOR is the distinction a rail needs, and
	 *  it is what makes the order derived rather than hand-arranged. */
	group: ToolGroup;
}

/** Reading order, top to bottom. The rail renders these bands in this sequence. */
export type ToolGroup = 'navigate' | 'draw' | 'assist';
export const TOOL_GROUPS: readonly ToolGroup[] = ['navigate', 'draw', 'assist'];

export const TOOL_DEFS: ToolDef[] = [
	// ── navigate: move around and pick things. Commits nothing, so no task rule ever hides these.
	{
		tool: 'select',
		icon: MousePointer2,
		label: 'Select',
		key: '1',
		drawing: false,
		group: 'navigate',
	},
	{ tool: 'pan', icon: Hand, label: 'Pan', key: '2', drawing: false, group: 'navigate' },
	{
		tool: 'lasso',
		icon: Lasso,
		label: 'Lasso (select)',
		key: '3',
		drawing: true,
		group: 'navigate',
	},
	// ── draw: commits a shape. Simple to elaborate, and each is hidden when the task refuses the
	//    shape it makes.
	{ tool: 'rect', icon: Square, label: 'Rectangle', key: '4', drawing: true, group: 'draw' },
	{ tool: 'polygon', icon: Pentagon, label: 'Polygon', key: '5', drawing: true, group: 'draw' },
	{ tool: 'point', icon: Crosshair, label: 'Point', key: '6', drawing: true, group: 'draw' },
	{ tool: 'line', icon: Minus, label: 'Line', key: '7', drawing: true, group: 'draw' },
	{
		tool: 'pencil',
		icon: PenLine,
		label: 'Pencil (freehand)',
		key: '8',
		drawing: true,
		group: 'draw',
	},
	{ tool: 'brush', icon: Paintbrush, label: 'Brush', key: '9', drawing: true, group: 'draw' },
	// ── assist: the machine does part of the drawing. A letter rather than a digit, because it is
	//    categorically different from the nine above and the rail says so.
	{
		tool: 'magnetic',
		icon: Magnet,
		label: 'Magnetic (corner-snap)',
		key: 'M',
		drawing: true,
		cv: true,
		group: 'assist',
	},
];

/** Split the tools that survived filtering into rail BANDS, in reading order.
 *
 *  Lives here rather than in the component so the test exercises the SAME function the rail renders.
 *  A test that re-implements the grouping can pass while the rail disagrees with it — the mirror is
 *  the bug, not the coverage.
 *
 *  EMPTY BANDS ARE DROPPED: the caller has already hidden the drawing tools the task refuses, so a
 *  bbox-only task empties `assist` entirely, and a separator rendered for an absent band is a
 *  hairline with nothing on either side of it.
 */
export function bandsOf(visible: readonly ToolDef[]): { group: ToolGroup; tools: ToolDef[] }[] {
	return TOOL_GROUPS.map((group) => ({
		group,
		tools: visible.filter((t) => t.group === group),
	})).filter((b) => b.tools.length > 0);
}

/** Hotkey → tool, derived from the registry (lowercased for the keydown handler). */
export const TOOL_KEYS: Record<string, Tool> = Object.fromEntries(
	TOOL_DEFS.map((t) => [t.key.toLowerCase(), t.tool]),
);

export const isCvTool = (tool: Tool): boolean =>
	TOOL_DEFS.some((t) => t.tool === tool && t.cv === true);
