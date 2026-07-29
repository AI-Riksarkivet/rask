import { Graphics } from 'pixi.js';
import { boundsFromPolygon, simplifyPath } from '../interaction/geometry.js';
import {
	type CommitShape,
	CURSOR_DRAW,
	type InteractionContext,
	STROKE_COLOR,
	type Tool,
} from '../interaction/types.js';

const MIN_DIST_SQ = 4; // 2px² — minimum squared distance between sampled points
const MIN_POINTS = 3; // need at least 3 vertices to form a shape

/**
 * Pencil — freehand drawing. Drag to trace a path, which is Douglas–Peucker
 * simplified on release. Commits a CLOSED polygon by default, or an OPEN
 * baseline (polyline) when Shift is held. `tolerance` is the simple↔complex knob:
 * higher = fewer vertices.
 */
export class PencilTool implements Tool {
	readonly name = 'pencil';
	readonly preview: Graphics;
	/** Simplification tolerance in world units (higher = simpler / fewer vertices). */
	tolerance = 2;

	private ctx: InteractionContext;
	private points: number[] = [];
	private drawing = false;

	onCommit?: (shape: CommitShape) => void;

	constructor(ctx: InteractionContext) {
		this.ctx = ctx;
		this.preview = new Graphics();
		this.preview.label = 'pencil-tool-preview';
		ctx.app.stage.addChild(this.preview);
	}

	onPointerDown(x: number, y: number): boolean {
		this.drawing = true;
		this.points = [x, y];
		this.ctx.setCursor(CURSOR_DRAW);
		return true;
	}

	onPointerMove(x: number, y: number): void {
		if (!this.drawing) return;
		const lastX = this.points[this.points.length - 2];
		const lastY = this.points[this.points.length - 1];
		const dx = x - lastX;
		const dy = y - lastY;
		if (dx * dx + dy * dy >= MIN_DIST_SQ) {
			this.points.push(x, y);
			this.renderPreview();
		}
	}

	onPointerUp(_x: number, _y: number): void {
		if (!this.drawing) return;
		this.drawing = false;

		const open = this.ctx.getModifiers().shift;
		const simplified = simplifyPath(this.points, this.tolerance);

		this.preview.clear();
		this.points = [];
		this.ctx.requestRender();

		if (simplified.length / 2 < MIN_POINTS) return;

		const bounds = boundsFromPolygon(simplified);
		this.onCommit?.({
			type: open ? 'baseline' : 'polygon',
			x: bounds.x,
			y: bounds.y,
			width: bounds.w,
			height: bounds.h,
			polygon: simplified,
		});
	}

	onDoubleClick(_x: number, _y: number): void {}

	onKeyDown(key: string): void {
		if (key === 'Escape') this.cancel();
	}

	cancel(): void {
		this.preview.clear();
		this.points = [];
		this.drawing = false;
		this.ctx.requestRender();
	}

	destroy(): void {
		this.preview.destroy();
	}

	private renderPreview(): void {
		this.preview.clear();
		if (this.points.length < 4) return;
		const scale = this.ctx.getViewportScale();
		const open = this.ctx.getModifiers().shift;
		this.preview.poly(this.points, !open);
		if (!open) this.preview.fill({ color: STROKE_COLOR, alpha: 0.06 });
		this.preview.stroke({ color: STROKE_COLOR, width: 1.5 / scale, alpha: 0.9 });
		this.ctx.requestRender();
	}
}
