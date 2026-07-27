import { Graphics } from 'pixi.js';
import { boundsFromPolygon } from '../interaction/geometry.js';
import {
	type CommitShape,
	type InteractionContext,
	STROKE_COLOR,
	type Tool,
} from '../interaction/types.js';

const MIN_LEN_SQ = 16; // 4px² minimum line length

/** Line — press-drag-release to draw a straight 2-point open line. */
export class LineTool implements Tool {
	readonly name = 'line';
	readonly preview: Graphics;
	private ctx: InteractionContext;
	private drawing = false;
	private x0 = 0;
	private y0 = 0;

	onCommit?: (shape: CommitShape) => void;

	constructor(ctx: InteractionContext) {
		this.ctx = ctx;
		this.preview = new Graphics();
		this.preview.label = 'line-tool-preview';
		ctx.app.stage.addChild(this.preview);
	}

	onPointerDown(x: number, y: number): boolean {
		this.drawing = true;
		this.x0 = x;
		this.y0 = y;
		return true;
	}

	onPointerMove(x: number, y: number): void {
		if (!this.drawing) return;
		this.preview.clear();
		this.preview.moveTo(this.x0, this.y0);
		this.preview.lineTo(x, y);
		this.preview.stroke({
			color: STROKE_COLOR,
			width: 2 / this.ctx.getViewportScale(),
		});
		this.ctx.requestRender();
	}

	onPointerUp(x: number, y: number): void {
		if (!this.drawing) return;
		this.drawing = false;
		this.preview.clear();
		this.ctx.requestRender();

		const dx = x - this.x0;
		const dy = y - this.y0;
		if (dx * dx + dy * dy < MIN_LEN_SQ) return;

		const pts = [this.x0, this.y0, x, y];
		const bounds = boundsFromPolygon(pts);
		this.onCommit?.({
			type: 'line',
			x: bounds.x,
			y: bounds.y,
			width: bounds.w,
			height: bounds.h,
			polygon: pts,
		});
	}

	onDoubleClick(_x: number, _y: number): void {}

	onKeyDown(key: string): void {
		if (key === 'Escape') this.cancel();
	}

	cancel(): void {
		this.preview.clear();
		this.drawing = false;
		this.ctx.requestRender();
	}

	destroy(): void {
		this.preview.destroy();
	}
}
