import { Graphics } from 'pixi.js';
import { type CommitShape, type InteractionContext, type Tool } from '../interaction/types.js';
import { boundsFromPolygon, simplifyPath, traceMaskContour } from '../interaction/geometry.js';

interface Stroke {
	erase: boolean;
	pts: number[]; // flat [x0,y0,x1,y1,...] in world coords
}

const MIN_DIST_SQ = 4;
const ADD_COLOR = 0x22c55e;
const ERASE_COLOR = 0xef4444;

/**
 * Brush — paint a raster mask. Drag to add (union of stamps along the path),
 * hold Alt (or set `erasing`) to subtract. Strokes accumulate into ONE mask;
 * double-click or Enter commits, Escape cancels.
 *
 * Within a mask, overlapping add-strokes are an idempotent boolean union;
 * eraser strokes are the inverse (destination-out). Masks are per-instance and
 * independent (instance segmentation — masks may overlap across annotations).
 *
 * The mask raster is rendered with a 2D offscreen canvas (deterministic
 * source-over / destination-out compositing) and exported as a base64 PNG of
 * the bounding-box crop. Live preview uses a Pixi stroke.
 */
export class BrushTool implements Tool {
	readonly name = 'brush';
	readonly preview: Graphics;
	/** Brush radius in world (image) units. */
	radius = 10;
	/** Eraser sub-mode (Alt held also erases). */
	erasing = false;
	/** "instance" = each commit is its own mask; "semantic" = one class per pixel (merged by the store). */
	maskMode: 'instance' | 'semantic' = 'instance';
	/** "mask" = commit a raster mask; "polygon" = vectorize the painted region to a polygon. */
	output: 'mask' | 'polygon' = 'mask';
	/** Douglas–Peucker tolerance for mask→polygon vectorization (higher = simpler). */
	simplifyTolerance = 1.5;

	private ctx: InteractionContext;
	private strokes: Stroke[] = [];
	private current: Stroke | null = null;

	onCommit?: (shape: CommitShape) => void;

	constructor(ctx: InteractionContext) {
		this.ctx = ctx;
		this.preview = new Graphics();
		this.preview.label = 'brush-tool-preview';
		ctx.app.stage.addChild(this.preview);
	}

	onPointerDown(x: number, y: number): boolean {
		const erase = this.erasing || this.ctx.getModifiers().alt;
		this.current = { erase, pts: [x, y] };
		this.strokes.push(this.current);
		this.renderPreview();
		return true;
	}

	onPointerMove(x: number, y: number): void {
		if (!this.current) return;
		const pts = this.current.pts;
		const dx = x - pts[pts.length - 2];
		const dy = y - pts[pts.length - 1];
		if (dx * dx + dy * dy >= MIN_DIST_SQ) {
			pts.push(x, y);
			this.renderPreview();
		}
	}

	onPointerUp(_x: number, _y: number): void {
		this.current = null;
	}

	onDoubleClick(_x: number, _y: number): void {
		this.commitMask();
	}

	onKeyDown(key: string): void {
		if (key === 'Escape') this.cancel();
		if (key === 'Enter') this.commitMask();
	}

	cancel(): void {
		this.strokes = [];
		this.current = null;
		this.preview.clear();
		this.ctx.requestRender();
	}

	destroy(): void {
		this.preview.destroy();
	}

	private bounds(): { x: number; y: number; w: number; h: number } | null {
		let minX = Infinity,
			minY = Infinity,
			maxX = -Infinity,
			maxY = -Infinity;
		let any = false;
		for (const s of this.strokes) {
			if (s.erase) continue;
			for (let i = 0; i < s.pts.length; i += 2) {
				any = true;
				minX = Math.min(minX, s.pts[i]);
				minY = Math.min(minY, s.pts[i + 1]);
				maxX = Math.max(maxX, s.pts[i]);
				maxY = Math.max(maxY, s.pts[i + 1]);
			}
		}
		if (!any) return null;
		const r = this.radius;
		return {
			x: Math.floor(minX - r),
			y: Math.floor(minY - r),
			w: Math.ceil(maxX - minX + r * 2),
			h: Math.ceil(maxY - minY + r * 2),
		};
	}

	private commitMask(): void {
		const b = this.bounds();
		if (!b || b.w < 1 || b.h < 1) {
			this.cancel();
			return;
		}

		const canvas = document.createElement('canvas');
		canvas.width = b.w;
		canvas.height = b.h;
		const octx = canvas.getContext('2d');
		if (!octx) {
			this.cancel();
			return;
		}

		octx.strokeStyle = '#ffffff';
		octx.fillStyle = '#ffffff';
		octx.lineCap = 'round';
		octx.lineJoin = 'round';
		octx.lineWidth = this.radius * 2;

		for (const s of this.strokes) {
			octx.globalCompositeOperation = s.erase ? 'destination-out' : 'source-over';
			octx.beginPath();
			octx.moveTo(s.pts[0] - b.x, s.pts[1] - b.y);
			if (s.pts.length <= 2) {
				// single tap → a dot
				octx.arc(s.pts[0] - b.x, s.pts[1] - b.y, this.radius, 0, Math.PI * 2);
				octx.fill();
			} else {
				for (let i = 2; i < s.pts.length; i += 2) {
					octx.lineTo(s.pts[i] - b.x, s.pts[i + 1] - b.y);
				}
				octx.stroke();
			}
		}

		const reset = () => {
			this.strokes = [];
			this.current = null;
			this.preview.clear();
			this.ctx.requestRender();
		};

		// Output = polygon → vectorize the painted region (contour trace + simplify).
		if (this.output === 'polygon') {
			const img = octx.getImageData(0, 0, b.w, b.h);
			const local = traceMaskContour(img.data, b.w, b.h, 128);
			if (local.length >= 6) {
				const world = new Array<number>(local.length);
				for (let i = 0; i < local.length; i += 2) {
					world[i] = local[i] + b.x;
					world[i + 1] = local[i + 1] + b.y;
				}
				const simplified = simplifyPath(world, this.simplifyTolerance);
				if (simplified.length >= 6) {
					const bb = boundsFromPolygon(simplified);
					reset();
					this.onCommit?.({
						type: 'polygon',
						x: bb.x,
						y: bb.y,
						width: bb.w,
						height: bb.h,
						polygon: simplified,
					});
					return;
				}
			}
			// Trace failed (e.g. eraser-only) → fall through to a raster mask.
		}

		const mask = canvas.toDataURL('image/png');
		reset();
		this.onCommit?.({
			type: 'mask',
			x: b.x,
			y: b.y,
			width: b.w,
			height: b.h,
			polygon: null,
			mask,
			maskMode: this.maskMode,
		});
	}

	private renderPreview(): void {
		this.preview.clear();
		for (const s of this.strokes) {
			const color = s.erase ? ERASE_COLOR : ADD_COLOR;
			if (s.pts.length >= 4) {
				this.preview.poly(s.pts, false);
				this.preview.stroke({
					color,
					width: this.radius * 2,
					alpha: 0.3,
					cap: 'round',
					join: 'round',
				});
			} else {
				this.preview.circle(s.pts[0], s.pts[1], this.radius);
				this.preview.fill({ color, alpha: 0.3 });
			}
		}
		this.ctx.requestRender();
	}
}
