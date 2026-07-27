import { Application } from 'pixi.js';
import type { ArrowDataPlugin } from '../pixi/ArrowDataPlugin.js';
import type { Tool as ToolType } from '../pixi/types.js';
import { RectEditor } from '../editors/RectEditor.js';
import { PolygonEditor } from '../editors/PolygonEditor.js';
import { MagneticTool } from '../tools/MagneticTool.js';
import { PolygonTool } from '../tools/PolygonTool.js';
import { RectTool } from '../tools/RectTool.js';
import { LassoTool } from '../tools/LassoTool.js';
import { PencilTool } from '../tools/PencilTool.js';
import { PointTool } from '../tools/PointTool.js';
import { LineTool } from '../tools/LineTool.js';
import { BrushTool } from '../tools/BrushTool.js';
import { isAxisAlignedRect } from './geometry.js';
import {
	type CommitShape,
	CURSOR_DRAW,
	type Editor,
	type GeometryUpdate,
	type InteractionContext,
	type Modifiers,
	type Tool,
} from './types.js';

export class InteractionManager {
	private app: Application;
	private arrowPlugin: ArrowDataPlugin;
	private tools = new Map<string, Tool>();
	private activeTool: Tool | null = null;
	private _toolName: ToolType = 'select';
	private brushTool!: BrushTool;

	private rectEditor: RectEditor;
	private polygonEditor: PolygonEditor;
	private activeEditor: Editor | null = null;

	private selectedIndex: number | null = null;
	private selectedSet = new Set<number>();
	private modifiers: Modifiers = { shift: false, ctrl: false, alt: false };
	private _editMode = false;

	// Pointer capture ID — tracked so we can release on pointerup/pointerleave
	private capturedPointerId: number | null = null;

	// Alt+Click cycling state
	private lastCycleX = -Infinity;
	private lastCycleY = -Infinity;
	private cycleHits: number[] = [];
	private cycleIndex = 0;

	onCommit?: (shape: CommitShape) => void;
	onSelect?: (index: number | null) => void;
	onChange?: (index: number, updates: GeometryUpdate) => void;
	onDirtyChange?: (hasDirtyEdits: boolean) => void;
	/** Fires when a CV tool's lazy init (corner detection over the still) completes. */
	onCvToolReady?: (tool: 'magnetic') => void;
	/** Fires when the magnetic cursor snaps onto / off a detected corner. */
	onMagneticSnap?: (snapped: boolean) => void;

	// The still image the magnetic tool reads pixels from. Set by the viewer after the
	// backdrop loads; null for video frames (CV tools stay unavailable).
	private imageSource: HTMLImageElement | null = null;
	// Tools whose (a-few-hundred-ms) initWithImage has been fired — one init per image.
	private readonly cvInitialized = new Set<string>();

	constructor(app: Application, arrowPlugin: ArrowDataPlugin) {
		this.app = app;
		this.arrowPlugin = arrowPlugin;

		const ctx = this.createContext();

		const rectTool = new RectTool(ctx);
		rectTool.onCommit = (shape) => this.onCommit?.(shape);
		this.tools.set('rect', rectTool);

		const polygonTool = new PolygonTool(ctx);
		polygonTool.onCommit = (shape) => this.onCommit?.(shape);
		this.tools.set('polygon', polygonTool);

		// NOTE: an IntelligentScissors (edge-trace) tool was removed 2026-07-21 — the
		// opencv-js segmentation_IntelligentScissorsMB binding pathfound degenerately
		// (1-point contours for every parameterization, verified in-browser on 5.0.0;
		// 4.12 failed to even initialize). @techstark/opencv-js itself is gone now
		// (3.9 MB gzipped for two ops — see tools/corners.ts); re-adding edge-trace means
		// implementing live-wire pathfinding here, not restoring the dependency.
		const magneticTool = new MagneticTool(ctx);
		magneticTool.onCommit = (shape) => this.onCommit?.(shape);
		magneticTool.onSnapChange = (snapped) => this.onMagneticSnap?.(snapped);
		this.tools.set('magnetic', magneticTool);

		const pencilTool = new PencilTool(ctx);
		pencilTool.onCommit = (shape) => this.onCommit?.(shape);
		this.tools.set('pencil', pencilTool);

		const pointTool = new PointTool(ctx);
		pointTool.onCommit = (shape) => this.onCommit?.(shape);
		this.tools.set('point', pointTool);

		const lineTool = new LineTool(ctx);
		lineTool.onCommit = (shape) => this.onCommit?.(shape);
		this.tools.set('line', lineTool);

		this.brushTool = new BrushTool(ctx);
		this.brushTool.onCommit = (shape) => this.onCommit?.(shape);
		this.tools.set('brush', this.brushTool);

		const lassoTool = new LassoTool(ctx, arrowPlugin);
		lassoTool.onLassoComplete = (indices) => {
			this.selectedSet.clear();
			for (const idx of indices) this.selectedSet.add(idx);
			const primary = indices.length > 0 ? indices[indices.length - 1] : null;
			this.selectedIndex = primary;
			this.arrowPlugin.highlightSet(this.selectedSet);
			this.onSelect?.(primary);
			// Keep lasso active — user can draw again without re-selecting the tool
		};
		this.tools.set('lasso', lassoTool);

		this.rectEditor = new RectEditor(ctx);
		// onChange fires every drag frame — DON'T update Arrow table here
		// The editor's renderHandles() provides visual feedback
		this.rectEditor.onChange = () => {};

		this.polygonEditor = new PolygonEditor(ctx);
		this.polygonEditor.onChange = () => {};

		this.setupEvents();
	}

	get currentTool(): ToolType {
		return this._toolName;
	}

	/** Current Brush options — the engine is the source of truth; the UI mirrors this. */
	get brushOptions(): {
		output: 'mask' | 'polygon';
		maskMode: 'instance' | 'semantic';
		erasing: boolean;
		radius: number;
	} {
		return {
			output: this.brushTool.output,
			maskMode: this.brushTool.maskMode,
			erasing: this.brushTool.erasing,
			radius: this.brushTool.radius,
		};
	}

	/** Provide the still image the CV tools read (call after the backdrop loads).
	 *  CV inits are lazy — nothing loads until a CV tool is first activated. */
	setImageSource(image: HTMLImageElement | null): void {
		this.imageSource = image;
		this.cvInitialized.clear(); // a new image needs fresh cost maps / keypoints
	}

	/** True when the CV tools can work here (a still image is loaded). */
	get cvCapable(): boolean {
		return this.imageSource !== null;
	}

	setTool(tool: ToolType): void {
		this.activeTool?.cancel();
		this._toolName = tool;
		this.activeTool = tool === 'select' || tool === 'pan' ? null : (this.tools.get(tool) ?? null);

		// Lazy-init the magnetic tool on first activation (corner detection is only
		// paid when used). The tool no-ops snapping until ready, so firing async is safe.
		if (tool === 'magnetic' && this.imageSource) {
			if (!this.cvInitialized.has(tool)) {
				this.cvInitialized.add(tool);
				const t = this.tools.get(tool) as MagneticTool;
				void t
					.initWithImage(this.imageSource)
					.then(() => this.onCvToolReady?.(tool))
					.catch((e: unknown) => {
						this.cvInitialized.delete(tool); // allow a retry on the next activation
						console.error(`${tool} tool init failed:`, e);
					});
			}
		}

		// Set cursor for the active tool
		if (tool === 'pan') {
			this.canvas.style.cursor = 'grab';
		} else {
			this.canvas.style.cursor = this.activeTool ? CURSOR_DRAW : 'default';
		}
	}

	/** Set edit mode — when false, no editing, no handles, view only */
	setEditMode(enabled: boolean): void {
		this._editMode = enabled;
		if (!enabled) {
			this.activeEditor?.detach();
			this.activeEditor = null;
		}
	}

	/** Set Brush tool options (radius, eraser, mask mode, vector/raster output). */
	setBrushOptions(opts: {
		radius?: number;
		erasing?: boolean;
		maskMode?: 'instance' | 'semantic';
		output?: 'mask' | 'polygon';
	}): void {
		if (opts.radius !== undefined) this.brushTool.radius = opts.radius;
		if (opts.erasing !== undefined) this.brushTool.erasing = opts.erasing;
		if (opts.maskMode !== undefined) this.brushTool.maskMode = opts.maskMode;
		if (opts.output !== undefined) this.brushTool.output = opts.output;
	}

	/** Update modifier key state — called by the page component's keyboard handler */
	setModifiers(shift: boolean, ctrl: boolean, alt: boolean): void {
		this.modifiers.shift = shift;
		this.modifiers.ctrl = ctrl;
		this.modifiers.alt = alt;
	}

	select(index: number | null, addToSelection = false): void {
		this.activeEditor?.detach();
		this.activeEditor = null;
		this.arrowPlugin.hover(null);

		if (addToSelection && index !== null) {
			// Ctrl+click: toggle in selection set
			if (this.selectedSet.has(index)) {
				this.selectedSet.delete(index);
				index = this.selectedSet.size > 0 ? [...this.selectedSet][this.selectedSet.size - 1] : null;
			} else {
				this.selectedSet.add(index);
			}
		} else if (index !== null) {
			// Normal click: replace selection
			this.selectedSet.clear();
			this.selectedSet.add(index);
		} else {
			this.selectedSet.clear();
		}

		this.selectedIndex = index;

		// Attach editor only in edit mode
		if (index !== null && this._editMode) {
			const { x, y, w, h, polygon } = this.arrowPlugin.getGeometry(index);
			const st = this.arrowPlugin.getShapeType(index);

			if (st === 'point') {
				// Points have no resizable geometry — no editor box.
			} else if (st === 'mask') {
				// Raster masks: a bounding-box editor lets you move/resize (and Delete)
				// the mask. The sprite follows the geometry override (see syncMasks).
				this.rectEditor.attach(index, x, y, w, h, null);
				this.activeEditor = this.rectEditor;
			} else if (
				st === 'line' ||
				st === 'baseline' ||
				(polygon && polygon.length >= 6 && !isAxisAlignedRect(polygon))
			) {
				// Open polylines + free-form polygons → vertex editor (NOT a rect resize box).
				this.polygonEditor.attach(index, x, y, w, h, polygon);
				this.activeEditor = this.polygonEditor;
			} else {
				// Axis-aligned rectangles + oriented boxes → rect resize editor.
				this.rectEditor.attach(index, x, y, w, h, null);
				this.activeEditor = this.rectEditor;
			}
		}

		this.arrowPlugin.highlightSet(this.selectedSet);
		this.onSelect?.(index);
	}

	getSelectedIndex(): number | null {
		return this.selectedIndex;
	}

	getSelectedSet(): ReadonlySet<number> {
		return this.selectedSet;
	}

	cancel(): void {
		this.activeTool?.cancel();
		this.activeEditor?.detach();
		this.activeEditor = null;
		this.selectedIndex = null;
		this.arrowPlugin.highlight(null);
	}

	updateHandles(): void {
		this.activeEditor?.renderHandles();
	}

	/** Convert selected rect to polygon (enables vertex editing) */
	convertToPolygon(): boolean {
		if (this.selectedIndex === null) return false;
		if (this.activeEditor !== this.rectEditor) return false;

		const { x, y, w, h } = this.arrowPlugin.getGeometry(this.selectedIndex);
		const polygon = [x, y, x + w, y, x + w, y + h, x, y + h];

		// Switch to polygon editor
		this.rectEditor.detach();
		this.polygonEditor.attach(this.selectedIndex, x, y, w, h, polygon);
		this.activeEditor = this.polygonEditor;

		// Emit change so the store gets the polygon points
		this.onChange?.(this.selectedIndex, { x, y, w, h, polygon });
		return true;
	}

	/** Forward key events from page component — tools handle Escape, Backspace */
	handleKeyDown(key: string): void {
		this.activeTool?.onKeyDown(key);
	}

	destroy(): void {
		this.removeEvents();
		for (const tool of this.tools.values()) tool.destroy();
		this.rectEditor.destroy();
		this.polygonEditor.destroy();
	}

	// --- Private ---

	/** Persist the active editor's geometry to the dirty overlay + re-render. */
	private persistActiveEditorGeometry(): void {
		if (this.selectedIndex === null || !this.activeEditor) return;
		const geo = this.activeEditor.getGeometry();
		if (!geo) return;
		this.arrowPlugin.setOverride(this.selectedIndex, {
			x: geo.x,
			y: geo.y,
			w: geo.w,
			h: geo.h,
			polygon: geo.polygon ?? [],
		});
		// Re-sync the batch renderer to show the updated geometry
		this.arrowPlugin.sync();
		// Restore highlight with updated geometry (sync clears it)
		this.arrowPlugin.highlight(this.selectedIndex);
		this.onDirtyChange?.(true);
		// Propagate the FINAL geometry (drag-end, not per-frame) so a binding layer can
		// persist it — the per-frame editor onChange stays no-op'd.
		this.onChange?.(this.selectedIndex, geo);
	}

	private createContext(): InteractionContext {
		return {
			app: this.app,
			screenToWorld: (sx, sy) => this.screenToWorld(sx, sy),
			worldToScreen: (wx, wy) => {
				const stage = this.app.stage;
				return {
					x: wx * stage.scale.x + stage.position.x,
					y: wy * stage.scale.y + stage.position.y,
				};
			},
			getViewportScale: () => this.app.stage.scale.x,
			getModifiers: () => this.modifiers,
			setCursor: (cursor: string) => {
				this.canvas.style.cursor = cursor;
			},
			requestRender: () => {
				this.app.render();
			},
		};
	}

	/** Single source of truth for coordinate transform */
	private screenToWorld(sx: number, sy: number): { x: number; y: number } {
		const stage = this.app.stage;
		return {
			x: (sx - stage.position.x) / stage.scale.x,
			y: (sy - stage.position.y) / stage.scale.y,
		};
	}

	private get canvas(): HTMLCanvasElement {
		return this.app.canvas as HTMLCanvasElement;
	}

	/**
	 * Convert pointer event to world coordinates.
	 * Always recompute getBoundingClientRect() — it's ~0.01ms and avoids
	 * stale-rect bugs when toolbar/sidebar shifts the canvas position
	 * without triggering a resize.
	 */
	private worldCoords(e: PointerEvent | MouseEvent): {
		x: number;
		y: number;
	} {
		const rect = this.canvas.getBoundingClientRect();
		return this.screenToWorld(e.clientX - rect.left, e.clientY - rect.top);
	}

	private setupEvents(): void {
		// Capture phase — fires BEFORE ImagePlugin's bubble-phase handlers
		// Must use stopImmediatePropagation (not stopPropagation) to block
		// bubble-phase listeners on the SAME element.
		this.canvas.addEventListener('pointerdown', this.onPointerDown, true);
		this.canvas.addEventListener('pointermove', this.onPointerMove, true);
		this.canvas.addEventListener('pointerup', this.onPointerUp, true);
		this.canvas.addEventListener('pointerleave', this.onPointerLeave, true);
		this.canvas.addEventListener('dblclick', this.onDoubleClick, true);
	}

	private removeEvents(): void {
		this.canvas.removeEventListener('pointerdown', this.onPointerDown, true);
		this.canvas.removeEventListener('pointermove', this.onPointerMove, true);
		this.canvas.removeEventListener('pointerup', this.onPointerUp, true);
		this.canvas.removeEventListener('pointerleave', this.onPointerLeave, true);
		this.canvas.removeEventListener('dblclick', this.onDoubleClick, true);
	}

	private onPointerDown = (e: PointerEvent): void => {
		if (e.button !== 0) return;
		// Sync modifiers from the actual event — robust against focus-dependent
		// keydown handlers (Alt+click vertex-delete, Ctrl+click multi-select, etc.).
		this.modifiers.shift = e.shiftKey;
		this.modifiers.ctrl = e.ctrlKey || e.metaKey;
		this.modifiers.alt = e.altKey;
		const { x, y } = this.worldCoords(e);

		// 1. Editor handle drag — with pointer capture for reliable drag
		if (this.activeEditor?.isAttached()) {
			const handle = this.activeEditor.hitTestHandle(x, y);
			if (handle) {
				e.stopImmediatePropagation();
				// Alt+click a polygon vertex → delete it (needs >3 vertices).
				if (
					this.modifiers.alt &&
					handle.type === 'vertex' &&
					this.activeEditor === this.polygonEditor
				) {
					if (this.polygonEditor.deleteVertex(handle.index)) {
						this.persistActiveEditorGeometry();
					}
					return;
				}
				this.arrowPlugin.highlight(null); // clear stale highlight during drag
				this.canvas.setPointerCapture(e.pointerId);
				this.capturedPointerId = e.pointerId;
				this.activeEditor.startDrag(handle, x, y);
				return;
			}
		}

		// 2. Drawing tool
		if (this.activeTool) {
			e.stopImmediatePropagation();
			this.activeTool.onPointerDown(x, y);
			return;
		}

		// 3. Select tool — click to select, then drag handles/body on next interaction
		if (this._toolName === 'select') {
			// Alt+Click: cycle through overlapping annotations at this point
			if (this.modifiers.alt) {
				const hits = this.arrowPlugin.getAllAnnotationsAtPoint(x, y);
				if (hits.length > 0) {
					e.stopImmediatePropagation();
					// Reset cycle if click position moved
					const dx = x - this.lastCycleX;
					const dy = y - this.lastCycleY;
					if (dx * dx + dy * dy > 25 || hits.length !== this.cycleHits.length) {
						this.cycleHits = hits;
						this.cycleIndex = 0;
					} else {
						this.cycleIndex = (this.cycleIndex + 1) % this.cycleHits.length;
					}
					this.lastCycleX = x;
					this.lastCycleY = y;
					this.select(this.cycleHits[this.cycleIndex]);
					return;
				}
			}

			const hit = this.arrowPlugin.getAnnotationAtPoint(x, y);
			if (hit !== null) {
				e.stopImmediatePropagation();
				const addToSelection = this.modifiers.ctrl;
				this.select(hit, addToSelection);
			} else {
				this.select(null);
			}
		}
	};

	private onPointerMove = (e: PointerEvent): void => {
		// Early return: nothing active and no editor → skip coord calculation
		const editorDragging = this.activeEditor?.isDragging();
		const hasActiveTool = this.activeTool !== null;
		const editorAttached = this.activeEditor?.isAttached();

		if (!editorDragging && !hasActiveTool && !editorAttached) {
			if (this._toolName === 'select') {
				const { x, y } = this.worldCoords(e);
				const hit = this.arrowPlugin.getAnnotationAtPoint(x, y);
				this.canvas.style.cursor = hit !== null ? 'pointer' : 'default';
				// Hover highlight — show light fill before clicking
				this.arrowPlugin.hover(hit !== this.selectedIndex ? hit : null);
			}
			return;
		}

		const { x, y } = this.worldCoords(e);

		if (editorDragging) {
			e.stopImmediatePropagation(); // block ImagePlugin pan on same element
			this.activeEditor!.drag(x, y);
			return;
		}

		if (hasActiveTool) {
			e.stopImmediatePropagation(); // block ImagePlugin pan on same element
			this.activeTool!.onPointerMove(x, y);
			return;
		}

		// Editor attached but not dragging — show handle cursors on hover
		if (editorAttached) {
			const handle = this.activeEditor!.hitTestHandle(x, y);
			if (!handle) {
				const hit = this.arrowPlugin.getAnnotationAtPoint(x, y);
				this.canvas.style.cursor = hit !== null ? 'pointer' : 'default';
			}
		}
	};

	private onPointerUp = (e: PointerEvent): void => {
		// Release pointer capture if we own it
		if (this.capturedPointerId !== null) {
			try {
				this.canvas.releasePointerCapture(this.capturedPointerId);
			} catch {
				/* pointer already released */
			}
			this.capturedPointerId = null;
		}

		if (this.activeEditor?.isDragging()) {
			e.stopImmediatePropagation(); // block ImagePlugin from resetting cursor
			const { x, y } = this.worldCoords(e);
			this.activeEditor.drag(x, y);
			this.activeEditor.endDrag();

			// Write to dirty overlay — Arrow table NOT rebuilt
			// Table only rebuilds on Save (when we POST to server anyway)
			this.persistActiveEditorGeometry();
			return;
		}

		if (this.activeTool) {
			e.stopImmediatePropagation();
			const { x, y } = this.worldCoords(e);
			this.activeTool.onPointerUp(x, y);
		}
	};

	/** End drag if pointer leaves canvas (safety net for non-captured events) */
	private onPointerLeave = (_e: PointerEvent): void => {
		// If we have pointer capture, events keep flowing — no action needed.
		// Only handle the case where drag is active WITHOUT capture (shouldn't happen, but safety).
		if (this.capturedPointerId !== null) return;
		if (this.activeEditor?.isDragging()) {
			this.activeEditor.endDrag();
		}
	};

	private onDoubleClick = (e: MouseEvent): void => {
		if (this.activeTool) {
			e.stopImmediatePropagation();
			const { x, y } = this.worldCoords(e);
			this.activeTool.onDoubleClick(x, y);
		}
	};
}
