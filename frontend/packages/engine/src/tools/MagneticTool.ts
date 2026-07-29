import { Graphics } from 'pixi.js';
import Flatbush from 'flatbush';
import { boundsFromPolygon, snapToPoint } from '../interaction/geometry.js';
import { detectCorners } from './corners.js';
import {
	type CommitShape,
	HANDLE_FILL,
	type InteractionContext,
	SNAP_THRESHOLD_PX,
	STROKE_COLOR,
	type Tool,
} from '../interaction/types.js';

/**
 * Magnetic Cursor tool — snaps to corner keypoints in the image. Shi–Tomasi corner
 * detection (`./corners.ts`, plain TypeScript) + a Flatbush spatial index.
 *
 * Flow:
 * 1. On init: detect corners in the still image
 * 2. Build Flatbush spatial index for fast nearest-neighbor queries
 * 3. As cursor moves, snap to nearest keypoint within threshold
 * 4. Click to place points (snapped or unsnapped)
 * 5. Double-click or snap-to-first to close polygon
 */
export class MagneticTool implements Tool {
	readonly name = 'magnetic';
	readonly preview: Graphics;
	private ctx: InteractionContext;

	// Keypoint index
	private keypointIndex: Flatbush | null = null;
	private keypoints: [number, number][] = [];
	private ready = false;

	// Drawing state
	private points: number[] = [];
	private cursorX = 0;
	private cursorY = 0;
	private isSnapped = false;
	private isClosable = false;

	onCommit?: (shape: CommitShape) => void;
	/** Fires when the cursor snaps onto / off a detected corner — UI shows snap state. */
	onSnapChange?: (snapped: boolean) => void;

	constructor(ctx: InteractionContext) {
		this.ctx = ctx;
		this.preview = new Graphics();
		this.preview.label = 'magnetic-tool-preview';
		ctx.app.stage.addChild(this.preview);
	}

	/** Initialize with image element — detect the corners the cursor will snap to.
	 *
	 *  Detection is a few hundred milliseconds of straight-line work over the pixel buffer
	 *  (217 ms at 1080p), so yield to the event loop first: the caller flips the toolbar into
	 *  its loading state immediately before awaiting this, and without a yield that state
	 *  would never paint. */
	async initWithImage(imageElement: HTMLImageElement): Promise<void> {
		await new Promise((resolve) => setTimeout(resolve, 0));

		const width = imageElement.naturalWidth;
		const height = imageElement.naturalHeight;
		const canvas = document.createElement('canvas');
		canvas.width = width;
		canvas.height = height;
		const ctx2d = canvas.getContext('2d', { willReadFrequently: false })!;
		ctx2d.drawImage(imageElement, 0, 0);

		this.keypoints = detectCorners(ctx2d.getImageData(0, 0, width, height).data, width, height);

		// Build spatial index
		if (this.keypoints.length > 0) {
			this.keypointIndex = new Flatbush(this.keypoints.length);
			for (const [x, y] of this.keypoints) {
				this.keypointIndex.add(x, y, x, y);
			}
			this.keypointIndex.finish();
		}

		this.ready = true;
	}

	get isDrawing(): boolean {
		return this.points.length >= 2;
	}

	onPointerDown(x: number, y: number): boolean {
		if (this.isClosable) {
			this.commitPolygon();
			return true;
		}

		// Use snapped cursor position
		const px = this.isSnapped ? this.cursorX : x;
		const py = this.isSnapped ? this.cursorY : y;

		if (this.points.length === 0) {
			this.points = [px, py];
		} else {
			this.points.push(px, py);
		}

		return true;
	}

	onPointerMove(x: number, y: number): void {
		const wasSnapped = this.isSnapped;
		// Snap to nearest keypoint
		if (this.keypointIndex && this.keypoints.length > 0) {
			const maxDist = 15 / this.ctx.getViewportScale();
			const neighbors = this.keypointIndex.neighbors(x, y, 1, maxDist);

			if (neighbors.length > 0) {
				const [kx, ky] = this.keypoints[neighbors[0]];
				this.cursorX = kx;
				this.cursorY = ky;
				this.isSnapped = true;
			} else {
				this.cursorX = x;
				this.cursorY = y;
				this.isSnapped = false;
			}
		} else {
			this.cursorX = x;
			this.cursorY = y;
			this.isSnapped = false;
		}
		if (this.isSnapped !== wasSnapped) this.onSnapChange?.(this.isSnapped);

		// Check snap-to-close
		if (this.points.length >= 6) {
			const threshold = SNAP_THRESHOLD_PX / this.ctx.getViewportScale();
			this.isClosable = snapToPoint(
				this.cursorX,
				this.cursorY,
				this.points[0],
				this.points[1],
				threshold,
			);
		} else {
			this.isClosable = false;
		}

		if (this.points.length >= 2) {
			this.renderPreview();
		}
	}

	onPointerUp(_x: number, _y: number): void {
		// Magnetic tool uses click (pointerdown), not drag
	}

	onDoubleClick(_x: number, _y: number): void {
		if (this.points.length >= 6) {
			this.commitPolygon();
		}
	}

	onKeyDown(key: string): void {
		if (key === 'Escape') this.cancel();
		if (key === 'Enter' && this.points.length >= 6) {
			// Commit without the double-click / snap-to-close gesture (parity with Polygon/Brush).
			this.commitPolygon();
		}
		if (key === 'Backspace' && this.points.length >= 4) {
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
		this.isSnapped = false;
	}

	destroy(): void {
		this.preview.destroy();
	}

	private commitPolygon(): void {
		if (this.points.length < 6) return;

		const bounds = boundsFromPolygon(this.points);
		this.preview.clear();
		this.ctx.requestRender(); // render-on-demand: repaint so the polygon doesn't ghost

		this.onCommit?.({
			type: 'polygon',
			x: bounds.x,
			y: bounds.y,
			width: bounds.w,
			height: bounds.h,
			polygon: [...this.points],
		});

		this.points = [];
		this.isClosable = false;
	}

	private renderPreview(): void {
		this.preview.clear();
		const scale = this.ctx.getViewportScale();
		const vertexR = 3 / scale;

		// Polygon edges + preview line to cursor
		const allPts = this.isClosable ? this.points : [...this.points, this.cursorX, this.cursorY];

		if (allPts.length >= 4) {
			this.preview.poly(allPts, this.isClosable);
			this.preview.fill({ color: STROKE_COLOR, alpha: 0.06 });
			this.preview.stroke({ color: STROKE_COLOR, width: 2 / scale });
		}

		// Vertex dots
		for (let i = 0; i < this.points.length; i += 2) {
			this.preview.circle(this.points[i], this.points[i + 1], vertexR);
			this.preview.fill({ color: HANDLE_FILL });
			this.preview.stroke({ color: STROKE_COLOR, width: 1 / scale });
		}

		// Cursor dot (larger when snapped)
		if (!this.isClosable) {
			const r = this.isSnapped ? 5 / scale : 3 / scale;
			this.preview.circle(this.cursorX, this.cursorY, r);
			this.preview.fill({
				color: this.isSnapped ? 0x22c55e : STROKE_COLOR,
				alpha: 0.8,
			});
			this.preview.stroke({ color: HANDLE_FILL, width: 1 / scale });
		}

		// Close indicator
		if (this.isClosable) {
			const r = 6 / scale;
			this.preview.circle(this.points[0], this.points[1], r);
			this.preview.fill({ color: STROKE_COLOR, alpha: 0.2 });
			this.preview.stroke({ color: STROKE_COLOR, width: 2 / scale });
		}

		this.ctx.requestRender();
	}
}
