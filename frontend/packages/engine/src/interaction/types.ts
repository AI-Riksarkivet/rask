import type { Application, Graphics } from 'pixi.js';
import type { HandlePosition } from './geometry.js';

// --- Shared constants ---

export const HANDLE_SIZE_PX = 4;
export const VERTEX_SIZE_PX = 3;
export const HIT_THRESHOLD_PX = 8;
export const SNAP_THRESHOLD_PX = 12;
export const STROKE_COLOR = 0x3b82f6;
export const HANDLE_FILL = 0xffffff;
export const MIDPOINT_FILL = 0xd4d4d8;
export const MIDPOINT_STROKE = 0xa1a1aa;

// --- Custom cursors (SVG data URIs) ---

/** Small circle with dot — for vertex grab */
function svgCursor(svg: string, size: number): string {
	const half = size / 2;
	const encoded = encodeURIComponent(svg);
	return `url("data:image/svg+xml,${encoded}") ${half} ${half}, auto`;
}

/** Blue circle — vertex handle hover */
export const CURSOR_VERTEX = svgCursor(
	`<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">` +
		`<circle cx="10" cy="10" r="7" fill="none" stroke="#3b82f6" stroke-width="2"/>` +
		`<circle cx="10" cy="10" r="2" fill="#3b82f6"/>` +
		`</svg>`,
	20,
);

/** Blue circle with + — midpoint (add vertex) */
export const CURSOR_MIDPOINT = svgCursor(
	`<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">` +
		`<circle cx="10" cy="10" r="7" fill="none" stroke="#3b82f6" stroke-width="1.5"/>` +
		`<line x1="10" y1="5" x2="10" y2="15" stroke="#3b82f6" stroke-width="2"/>` +
		`<line x1="5" y1="10" x2="15" y2="10" stroke="#3b82f6" stroke-width="2"/>` +
		`</svg>`,
	20,
);

/** Thin crosshair — for drawing tools */
export const CURSOR_DRAW = svgCursor(
	`<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">` +
		`<line x1="12" y1="0" x2="12" y2="10" stroke="#333" stroke-width="1"/>` +
		`<line x1="12" y1="14" x2="12" y2="24" stroke="#333" stroke-width="1"/>` +
		`<line x1="0" y1="12" x2="10" y2="12" stroke="#333" stroke-width="1"/>` +
		`<line x1="14" y1="12" x2="24" y2="12" stroke="#333" stroke-width="1"/>` +
		`</svg>`,
	24,
);

// --- Handle identifier (typed, not stringly) ---

export type HandleId =
	| { type: 'rect'; position: HandlePosition }
	| { type: 'vertex'; index: number }
	| { type: 'midpoint'; fromIndex: number; toIndex: number }
	| { type: 'body' };

export function handleIdToString(id: HandleId): string {
	switch (id.type) {
		case 'rect':
			return id.position;
		case 'vertex':
			return `v${id.index}`;
		case 'midpoint':
			return `m${id.fromIndex}_${id.toIndex}`;
		case 'body':
			return 'body';
	}
}

// --- Shapes ---

export interface CommitShape {
	type: 'rect' | 'rotation' | 'polygon' | 'baseline' | 'line' | 'point' | 'mask';
	x: number;
	y: number;
	width: number;
	height: number;
	polygon: number[] | null;
	/** Oriented-box angle in radians (only for type "rotation"). */
	rotation?: number;
	/** base64 PNG data URL of the painted region (only for type "mask"). */
	mask?: string;
	/** How a committed mask is merged: independent ("instance") vs one-class-per-pixel ("semantic"). */
	maskMode?: 'instance' | 'semantic';
}

export interface GeometryUpdate {
	x: number;
	y: number;
	w: number;
	h: number;
	polygon: number[] | null;
}

// --- Context ---

export interface InteractionContext {
	app: Application;
	screenToWorld: (sx: number, sy: number) => { x: number; y: number };
	worldToScreen: (wx: number, wy: number) => { x: number; y: number };
	getViewportScale: () => number;
	getModifiers: () => Modifiers;
	setCursor: (cursor: string) => void;
	requestRender: () => void;
}

export interface Modifiers {
	shift: boolean;
	ctrl: boolean;
	alt: boolean;
}

// --- Interfaces ---

export interface Tool {
	readonly name: string;
	readonly preview: Graphics;

	onPointerDown(x: number, y: number): boolean;
	onPointerMove(x: number, y: number): void;
	onPointerUp(x: number, y: number): void;
	onDoubleClick(x: number, y: number): void;
	onKeyDown(key: string): void;
	cancel(): void;
	destroy(): void;

	onCommit?: (shape: CommitShape) => void;
}

export interface Editor {
	attach(
		index: number,
		x: number,
		y: number,
		w: number,
		h: number,
		polygon: ArrayLike<number> | null,
	): void;
	detach(): void;
	isAttached(): boolean;

	hitTestHandle(x: number, y: number): HandleId | null;
	startDrag(handle: HandleId, x: number, y: number): void;
	drag(x: number, y: number): void;
	endDrag(): void;
	isDragging(): boolean;

	/** Get the current edited geometry (call after endDrag to commit) */
	getGeometry(): GeometryUpdate | null;

	renderHandles(): void;
	destroy(): void;

	onChange?: (index: number, updates: GeometryUpdate) => void;
}
