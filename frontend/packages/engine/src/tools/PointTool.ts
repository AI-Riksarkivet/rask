import { Graphics } from 'pixi.js';
import {
	type CommitShape,
	type InteractionContext,
	STROKE_COLOR,
	type Tool,
} from '../interaction/types.js';

/** Point — click to drop a single keypoint (x,y). Stored as a 1-vertex polygon. */
export class PointTool implements Tool {
	readonly name = 'point';
	readonly preview: Graphics;
	private ctx: InteractionContext;

	onCommit?: (shape: CommitShape) => void;

	constructor(ctx: InteractionContext) {
		this.ctx = ctx;
		this.preview = new Graphics();
		this.preview.label = 'point-tool-preview';
		ctx.app.stage.addChild(this.preview);
	}

	onPointerDown(_x: number, _y: number): boolean {
		return true;
	}

	onPointerMove(x: number, y: number): void {
		// Crosshair preview at cursor
		this.preview.clear();
		const r = 5 / this.ctx.getViewportScale();
		this.preview.circle(x, y, r);
		this.preview.stroke({ color: STROKE_COLOR, width: 1.5 / this.ctx.getViewportScale() });
		this.ctx.requestRender();
	}

	onPointerUp(x: number, y: number): void {
		this.preview.clear();
		this.ctx.requestRender();
		this.onCommit?.({
			type: 'point',
			x,
			y,
			width: 0,
			height: 0,
			polygon: [x, y],
		});
	}

	onDoubleClick(_x: number, _y: number): void {}

	onKeyDown(key: string): void {
		if (key === 'Escape') this.cancel();
	}

	cancel(): void {
		this.preview.clear();
		this.ctx.requestRender();
	}

	destroy(): void {
		this.preview.destroy();
	}
}
