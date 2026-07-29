import { Graphics } from 'pixi.js';
import { boundsFromPolygon, snapToPoint } from '../interaction/geometry.js';
import {
	type CommitShape,
	HANDLE_FILL,
	type InteractionContext,
	SNAP_THRESHOLD_PX,
	STROKE_COLOR,
	type Tool,
} from '../interaction/types.js';

const MIN_VERTICES = 3;

export class PolygonTool implements Tool {
	readonly name = 'polygon';
	readonly preview: Graphics;
	private ctx: InteractionContext;
	private points: number[] = [];
	private cursorX = 0;
	private cursorY = 0;
	private isClosable = false;

	onCommit?: (shape: CommitShape) => void;

	constructor(ctx: InteractionContext) {
		this.ctx = ctx;
		this.preview = new Graphics();
		this.preview.label = 'polygon-tool-preview';
		ctx.app.stage.addChild(this.preview);
	}

	get isDrawing(): boolean {
		return this.points.length >= 2;
	}

	onPointerDown(x: number, y: number): boolean {
		// First point: start drawing
		if (this.points.length === 0) {
			this.points = [x, y];
			return true;
		}

		// Check snap-to-close
		const threshold = SNAP_THRESHOLD_PX / this.ctx.getViewportScale();
		if (
			this.points.length >= MIN_VERTICES * 2 &&
			snapToPoint(x, y, this.points[0], this.points[1], threshold)
		) {
			this.commitPolygon();
			return true;
		}

		// Add vertex
		this.points.push(x, y);
		return true;
	}

	onPointerMove(x: number, y: number): void {
		this.cursorX = x;
		this.cursorY = y;

		if (this.points.length < 2) return;

		// Check if close enough to snap
		const threshold = SNAP_THRESHOLD_PX / this.ctx.getViewportScale();
		this.isClosable =
			this.points.length >= MIN_VERTICES * 2 &&
			snapToPoint(x, y, this.points[0], this.points[1], threshold);

		this.renderPreview();
	}

	onPointerUp(_x: number, _y: number): void {
		// Polygon uses click (pointerdown), not drag
	}

	onDoubleClick(_x: number, _y: number): void {
		if (this.points.length >= MIN_VERTICES * 2) {
			this.commitPolygon();
		}
	}

	onKeyDown(key: string): void {
		if (key === 'Escape') {
			this.cancel();
		}
		if (key === 'Enter' && this.points.length >= MIN_VERTICES * 2) {
			// Commit without the double-click / snap-to-close gesture (parity with Brush).
			this.commitPolygon();
		}
		if (key === 'Backspace' && this.points.length >= 4) {
			// Remove last vertex
			this.points.pop();
			this.points.pop();
			this.renderPreview();
		}
	}

	cancel(): void {
		this.preview.clear();
		this.ctx.requestRender();
		this.points = [];
		this.isClosable = false;
	}

	destroy(): void {
		this.preview.destroy();
	}

	private commitPolygon(): void {
		const pts = [...this.points];
		const bounds = boundsFromPolygon(pts);

		this.preview.clear();
		this.ctx.requestRender(); // render-on-demand: repaint so the polyline doesn't ghost
		this.points = [];
		this.isClosable = false;

		this.onCommit?.({
			type: 'polygon',
			x: bounds.x,
			y: bounds.y,
			width: bounds.w,
			height: bounds.h,
			polygon: pts,
		});
	}

	private renderPreview(): void {
		this.preview.clear();
		const scale = this.ctx.getViewportScale();
		const vertexR = 4 / scale;

		// Solid edges between committed vertices
		if (this.points.length >= 4) {
			this.preview.poly(this.points, false);
			this.preview.stroke({ color: STROKE_COLOR, width: 2 / scale });
		}

		// Dashed line from last vertex to cursor
		const lastX = this.points[this.points.length - 2];
		const lastY = this.points[this.points.length - 1];
		this.preview.moveTo(lastX, lastY);
		this.preview.lineTo(this.cursorX, this.cursorY);
		this.preview.stroke({ color: STROKE_COLOR, width: 1 / scale, alpha: 0.5 });

		// Vertex dots
		for (let i = 0; i < this.points.length; i += 2) {
			this.preview.circle(this.points[i], this.points[i + 1], vertexR);
			this.preview.fill({ color: HANDLE_FILL });
			this.preview.stroke({ color: STROKE_COLOR, width: 1.5 / scale });
		}

		// Snap-to-close highlight on first vertex
		if (this.isClosable) {
			const closeR = 8 / scale;
			this.preview.circle(this.points[0], this.points[1], closeR);
			this.preview.fill({ color: STROKE_COLOR, alpha: 0.2 });
			this.preview.stroke({ color: STROKE_COLOR, width: 2 / scale });
		}

		this.ctx.requestRender();
	}
}
