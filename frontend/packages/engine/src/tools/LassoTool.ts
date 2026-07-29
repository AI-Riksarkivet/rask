import { Graphics } from 'pixi.js';
import { pointInPolygon } from '../interaction/geometry.js';
import {
	type CommitShape,
	CURSOR_DRAW,
	type InteractionContext,
	STROKE_COLOR,
	type Tool,
} from '../interaction/types.js';
import type { ArrowDataPlugin } from '../pixi/ArrowDataPlugin.js';

const MIN_DIST_SQ = 9; // 3px² — minimum squared distance between sampled points

export class LassoTool implements Tool {
	readonly name = 'lasso';
	readonly preview: Graphics;
	private ctx: InteractionContext;
	private arrowPlugin: ArrowDataPlugin;
	private points: number[] = [];
	private drawing = false;

	onCommit?: (shape: CommitShape) => void;
	onLassoComplete?: (indices: number[]) => void;

	constructor(ctx: InteractionContext, arrowPlugin: ArrowDataPlugin) {
		this.ctx = ctx;
		this.arrowPlugin = arrowPlugin;
		this.preview = new Graphics();
		this.preview.label = 'lasso-preview';
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
		}
		this.renderPreview();
	}

	onPointerUp(_x: number, _y: number): void {
		if (!this.drawing) return;
		this.drawing = false;

		if (this.points.length < 6) {
			this.cancel();
			return;
		}

		const indices = this.findAnnotationsInside();
		this.preview.clear();
		this.points = [];
		this.ctx.requestRender();
		this.onLassoComplete?.(indices);
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

	private findAnnotationsInside(): number[] {
		const lasso = this.points;
		const results: number[] = [];
		const n = this.arrowPlugin.getNumRows();
		for (let i = 0; i < n; i++) {
			if (!this.arrowPlugin.isVisible(i)) continue;
			const geo = this.arrowPlugin.getGeometry(i);
			const cx = geo.x + geo.w / 2;
			const cy = geo.y + geo.h / 2;
			if (pointInPolygon(cx, cy, lasso)) {
				results.push(i);
			}
		}
		return results;
	}

	private renderPreview(): void {
		this.preview.clear();
		if (this.points.length < 4) return;
		const scale = this.ctx.getViewportScale();
		// Stroke the path
		this.preview.poly(this.points, false);
		this.preview.stroke({
			color: STROKE_COLOR,
			width: 1.5 / scale,
			alpha: 0.8,
		});
		// Light fill to show lasso area
		this.preview.poly(this.points, true);
		this.preview.fill({ color: STROKE_COLOR, alpha: 0.04 });
		this.ctx.requestRender();
	}
}
