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
}

export const TOOL_DEFS: ToolDef[] = [
	{ tool: 'select', icon: MousePointer2, label: 'Select', key: '1', drawing: false },
	{ tool: 'pan', icon: Hand, label: 'Pan', key: '2', drawing: false },
	{ tool: 'rect', icon: Square, label: 'Rectangle', key: '3', drawing: true },
	{ tool: 'polygon', icon: Pentagon, label: 'Polygon', key: '4', drawing: true },
	{ tool: 'point', icon: Crosshair, label: 'Point', key: '5', drawing: true },
	{ tool: 'line', icon: Minus, label: 'Line', key: '6', drawing: true },
	{ tool: 'lasso', icon: Lasso, label: 'Lasso (select)', key: '7', drawing: true },
	{ tool: 'pencil', icon: PenLine, label: 'Pencil (freehand)', key: '8', drawing: true },
	{
		tool: 'magnetic',
		icon: Magnet,
		label: 'Magnetic (corner-snap)',
		key: '9',
		drawing: true,
		cv: true,
	},
	{ tool: 'brush', icon: Paintbrush, label: 'Brush', key: 'B', drawing: true },
];

/** Hotkey → tool, derived from the registry (lowercased for the keydown handler). */
export const TOOL_KEYS: Record<string, Tool> = Object.fromEntries(
	TOOL_DEFS.map((t) => [t.key.toLowerCase(), t.tool]),
);

export const isCvTool = (tool: Tool): boolean =>
	TOOL_DEFS.some((t) => t.tool === tool && t.cv === true);
