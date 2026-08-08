import { Graphics } from 'pixi.js';
import { computeRect } from '../interaction/geometry.js';
import {
	type CommitShape,
	type InteractionContext,
	STROKE_COLOR,
	type Tool,
} from '../interaction/types.js';

export class RectTool implements Tool {
	readonly name = 'rect';
	readonly preview: Graphics;
	private ctx: InteractionContext;
	private drawing = false;
	private originX = 0;
	private originY = 0;

	onCommit?: (shape: CommitShape) => void;

	constructor(ctx: InteractionContext) {
		this.ctx = ctx;
		this.preview = new Graphics();
		this.preview.label = 'rect-tool-preview';
		ctx.app.stage.addChild(this.preview);
	}

	onPointerDown(x: number, y: number): boolean {
		this.drawing = true;
		this.originX = x;
		this.originY = y;
		return true;
	}

	onPointerMove(x: number, y: number): void {
		if (!this.drawing) return;

		const { shift, ctrl } = this.ctx.getModifiers();
		const r = computeRect(this.originX, this.originY, x, y, shift, ctrl);

		this.preview.clear();
		this.preview.rect(r.x, r.y, r.w, r.h);
		this.preview.fill({ color: STROKE_COLOR, alpha: 0.1 });
		this.preview.stroke({ color: STROKE_COLOR, width: 2 });
		this.ctx.requestRender();
	}

	onPointerUp(x: number, y: number): void {
		if (!this.drawing) return;
		this.drawing = false;

		const { shift, ctrl } = this.ctx.getModifiers();
		const r = computeRect(this.originX, this.originY, x, y, shift, ctrl);

		this.preview.clear();
		this.ctx.requestRender(); // render-on-demand: repaint so the rubber-band doesn't ghost

		// ALWAYS commit — a click included (a zero-size rect at the point). Whether a tiny rect is
		// an accidental click to discard or a REGION PROMPT to honour (SAM's click-to-segment grows
		// a zero box into a patch server-side) is the consumer's call: a MIN_AREA gate here decided
		// it for everyone, and made the promised click-to-segment unreachable from the only tool
		// that could send it. Store as 4-point polygon — rect is just a polygon with 4 corners.
		this.onCommit?.({
			type: 'rect',
			x: r.x,
			y: r.y,
			width: r.w,
			height: r.h,
			polygon: [r.x, r.y, r.x + r.w, r.y, r.x + r.w, r.y + r.h, r.x, r.y + r.h],
		});
	}

	onDoubleClick(_x: number, _y: number): void {
		// Not used for rect tool
	}

	onKeyDown(key: string): void {
		if (key === 'Escape') this.cancel();
	}

	cancel(): void {
		this.preview.clear();
		this.ctx.requestRender();
		this.drawing = false;
	}

	destroy(): void {
		this.preview.destroy();
	}
}
