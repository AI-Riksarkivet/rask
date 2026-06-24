export interface Transform {
	x: number;
	y: number;
	scale: number;
}

export interface CanvasOptions {
	onAfterDraw?: (ctx: CanvasRenderingContext2D, transform: Transform) => void;
}

const MIN_SCALE = 0.1;
const MAX_SCALE = 10;
const ZOOM_LERP = 0.25;
const PAN_FRICTION = 0.92;

export class CanvasController {
	private canvas: HTMLCanvasElement;
	private ctx: CanvasRenderingContext2D;
	private transform: Transform = { x: 0, y: 0, scale: 1 };
	private targetScale = 1;
	private img: HTMLImageElement | null = null;
	private options: CanvasOptions;
	private animId = 0;
	private isDragging = false;
	private dragStart = { x: 0, y: 0 };
	private dragTransformStart = { x: 0, y: 0 };
	private velocity = { x: 0, y: 0 };
	private lastPointer = { x: 0, y: 0, time: 0 };
	private observer: IntersectionObserver | null = null;
	private visible = true;
	private resizeObserver: ResizeObserver | null = null;
	panEnabled = true;

	constructor(canvas: HTMLCanvasElement, options: CanvasOptions = {}) {
		this.canvas = canvas;
		this.ctx = canvas.getContext('2d')!;
		this.options = options;

		canvas.addEventListener('pointerdown', this.onPointerDown);
		canvas.addEventListener('pointermove', this.onPointerMove);
		canvas.addEventListener('pointerup', this.onPointerUp);
		canvas.addEventListener('pointercancel', this.onPointerUp);
		canvas.addEventListener('wheel', this.onWheel, { passive: false });

		this.observer = new IntersectionObserver(([entry]) => {
			if (!entry) return;
			this.visible = entry.isIntersecting;
			if (this.visible) this.scheduleRender();
		});
		this.observer.observe(canvas);

		this.resizeObserver = new ResizeObserver(() => this.render());
		this.resizeObserver.observe(canvas);

		this.scheduleRender();
	}

	setImage(img: HTMLImageElement) {
		this.img = img;
		this.fitToCanvas();
		this.render();
	}

	fitToCanvas() {
		if (!this.img) return;
		const rect = this.canvas.getBoundingClientRect();
		const pad = 8;
		const scaleX = (rect.width - pad * 2) / this.img.naturalWidth;
		const scaleY = (rect.height - pad * 2) / this.img.naturalHeight;
		const scale = Math.min(scaleX, scaleY);
		this.transform.scale = scale;
		this.targetScale = scale;
		this.transform.x = (rect.width - this.img.naturalWidth * scale) / 2;
		this.transform.y = (rect.height - this.img.naturalHeight * scale) / 2;
	}

	/** Zoom and pan to fit a rectangle (in image coordinates) with padding */
	fitToRect(x: number, y: number, w: number, h: number) {
		const rect = this.canvas.getBoundingClientRect();
		const pad = 40;
		const scaleX = (rect.width - pad * 2) / w;
		const scaleY = (rect.height - pad * 2) / h;
		const scale = Math.min(scaleX, scaleY, MAX_SCALE);
		this.transform.scale = scale;
		this.targetScale = scale;
		this.transform.x = (rect.width - w * scale) / 2 - x * scale;
		this.transform.y = (rect.height - h * scale) / 2 - y * scale;
	}

	render() {
		this.scheduleRender();
	}

	/** Convert a canvas-space pixel coordinate to image-space. */
	canvasToImage(canvasX: number, canvasY: number): { x: number; y: number } {
		return {
			x: (canvasX - this.transform.x) / this.transform.scale,
			y: (canvasY - this.transform.y) / this.transform.scale,
		};
	}

	destroy() {
		cancelAnimationFrame(this.animId);
		this.canvas.removeEventListener('pointerdown', this.onPointerDown);
		this.canvas.removeEventListener('pointermove', this.onPointerMove);
		this.canvas.removeEventListener('pointerup', this.onPointerUp);
		this.canvas.removeEventListener('pointercancel', this.onPointerUp);
		this.canvas.removeEventListener('wheel', this.onWheel);
		this.observer?.disconnect();
		this.resizeObserver?.disconnect();
	}

	private scheduleRender() {
		cancelAnimationFrame(this.animId);
		this.animId = requestAnimationFrame(() => this.draw());
	}

	private draw() {
		if (!this.visible) return;

		const dpr = window.devicePixelRatio || 1;
		const rect = this.canvas.getBoundingClientRect();
		this.canvas.width = rect.width * dpr;
		this.canvas.height = rect.height * dpr;

		const ctx = this.ctx;
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.clearRect(0, 0, rect.width, rect.height);

		// Smooth zoom
		const scaleDiff = this.targetScale - this.transform.scale;
		if (Math.abs(scaleDiff) > 0.001) {
			this.transform.scale += scaleDiff * ZOOM_LERP;
			this.scheduleRender();
		}

		// Pan inertia
		if (Math.abs(this.velocity.x) > 0.5 || Math.abs(this.velocity.y) > 0.5) {
			this.transform.x += this.velocity.x;
			this.transform.y += this.velocity.y;
			this.velocity.x *= PAN_FRICTION;
			this.velocity.y *= PAN_FRICTION;
			this.scheduleRender();
		}

		ctx.save();
		ctx.translate(this.transform.x, this.transform.y);
		ctx.scale(this.transform.scale, this.transform.scale);

		if (this.img) {
			ctx.drawImage(this.img, 0, 0);
		}

		this.options.onAfterDraw?.(ctx, this.transform);

		ctx.restore();
	}

	private onPointerDown = (e: PointerEvent) => {
		if (!this.panEnabled) return;
		this.isDragging = true;
		this.velocity = { x: 0, y: 0 };
		this.dragStart = { x: e.clientX, y: e.clientY };
		this.dragTransformStart = { x: this.transform.x, y: this.transform.y };
		this.lastPointer = { x: e.clientX, y: e.clientY, time: Date.now() };
		this.canvas.setPointerCapture(e.pointerId);
	};

	private onPointerMove = (e: PointerEvent) => {
		if (!this.isDragging || !this.panEnabled) return;
		const dx = e.clientX - this.dragStart.x;
		const dy = e.clientY - this.dragStart.y;
		this.transform.x = this.dragTransformStart.x + dx;
		this.transform.y = this.dragTransformStart.y + dy;

		const now = Date.now();
		const dt = now - this.lastPointer.time;
		if (dt > 0) {
			this.velocity.x = (e.clientX - this.lastPointer.x) * (16 / dt);
			this.velocity.y = (e.clientY - this.lastPointer.y) * (16 / dt);
		}
		this.lastPointer = { x: e.clientX, y: e.clientY, time: now };

		this.scheduleRender();
	};

	private onPointerUp = (e: PointerEvent) => {
		this.isDragging = false;
		this.canvas.releasePointerCapture(e.pointerId);
		this.scheduleRender();
	};

	private onWheel = (e: WheelEvent) => {
		e.preventDefault();
		let delta = -e.deltaY;
		if (e.deltaMode === 1) delta *= 16;

		const factor = Math.exp(delta * 0.003);
		const newScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, this.targetScale * factor));

		// Zoom centered on cursor
		const rect = this.canvas.getBoundingClientRect();
		const cx = e.clientX - rect.left;
		const cy = e.clientY - rect.top;
		const ratio = newScale / this.transform.scale;
		this.transform.x = cx - (cx - this.transform.x) * ratio;
		this.transform.y = cy - (cy - this.transform.y) * ratio;

		this.targetScale = newScale;
		this.transform.scale = newScale; // apply immediately for wheel zoom anchor accuracy
		this.scheduleRender();
	};
}
