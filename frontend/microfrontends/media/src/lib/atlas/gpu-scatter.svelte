<script lang="ts">
	/**
	 * gpu-scatter — a self-contained WebGPU point-scatter renderer for the atlas.
	 *
	 * Modern WebGPU only: the point renderer uses instanced quads (WebGPU has no
	 * point-size primitive) shaded into round, premultiplied-alpha sprites. The
	 * component owns ONLY pixels + camera + pointer gestures; all data/colour/
	 * selection logic stays in AtlasMap, which feeds us split GPU data and
	 * receives DATA-space callbacks.
	 *
	 * DATA SPLIT (for cheap slider dimming — see #2): instead of one premultiplied
	 * RGBA buffer rebuilt on every filterAlpha tick, AtlasMap feeds:
	 *   • xBits/yBits — raw float16 position bits (the GPU vertex buffer, #3);
	 *   • baseRgb     — per-point category hue (rebuilt only on colorBy/theme);
	 *   • state       — 1 byte/point membership enum (rebuilt only on filter/
	 *                   selection/hidden); 0=NORMAL 1=NOISE 2=DIMMED 3=HIDDEN;
	 *   • filterAlpha/noiseAlpha — uniforms the shader applies per state.
	 * A slider drag now writes ONE 4-byte uniform region, not a 145k JS buffer.
	 *
	 *   • onHover(dataX, dataY)  — rAF-throttled cursor → data coords
	 *   • onHoverEnd()           — pointer left the canvas
	 *   • onPick(dataX, dataY)   — a click (drag-free) at data coords
	 *   • onLasso(flatDataXY)    — lasso polygon vertices in DATA coords
	 *
	 * Camera is data-space pan/zoom (cursor-anchored wheel). The lasso draws on a
	 * separate 2D overlay so the GPU surface is never disturbed mid-gesture.
	 */

	type Mode = 'lasso' | 'pan';
	type FitBounds = { minX: number; minY: number; maxX: number; maxY: number };

	let {
		xBits,
		yBits,
		fitBounds,
		baseRgb,
		// aliased: a bare `state` binding shadows the `$state` rune (svelte error).
		state: pointState,
		filterAlpha,
		noiseAlpha,
		pointSize,
		width,
		height,
		background,
		onHover,
		onHoverEnd,
		onPick,
		onLasso,
		mode,
		markerXY = null,
	}: {
		xBits: Uint16Array; // raw float16 bits, length count — GPU position X
		yBits: Uint16Array; // raw float16 bits, length count — GPU position Y
		fitBounds: FitBounds; // data extent (from decoded f32 in AtlasMap) for fit()
		baseRgb: Uint8Array; // length 3*count or 4*count, per-point category RGB hue
		state: Uint8Array; // length count, membership enum (0 NORMAL / 1 NOISE / 2 DIMMED / 3 HIDDEN)
		filterAlpha: number; // 0..120 byte-scale opacity of DIMMED points (slider)
		noiseAlpha: number; // 0..255 byte-scale opacity of NOISE points (const)
		pointSize: number; // device px diameter; clamped to a sane minimum
		width: number; // css px
		height: number; // css px
		background: string; // css hex, e.g. '#0a0a0a'
		onHover: (dataX: number, dataY: number) => void;
		onHoverEnd: () => void;
		onPick: (dataX: number, dataY: number) => void;
		onLasso: (polyDataXY: number[]) => void; // flat [x0,y0,x1,y1,...] in DATA coords
		mode: Mode;
		markerXY?: [number, number] | null; // active point in DATA coords → highlight ring
	} = $props();

	const dpr = Math.min(globalThis.devicePixelRatio ?? 1, 1.5);

	let gpuCanvas = $state<HTMLCanvasElement | null>(null);
	let overlayCanvas = $state<HTMLCanvasElement | null>(null);
	let gpuError = $state<string | null>(null);

	// ── camera (data coords at canvas centre + device-px per data unit) ───────
	let center = $state.raw<[number, number]>([0, 0]);
	let scale = $state(1);

	// ── GPU handles (plain refs; not reactive) ────────────────────────────────
	let device: GPUDevice | null = null;
	let ctx: GPUCanvasContext | null = null;
	let pipeline: GPURenderPipeline | null = null;
	let uniformBuffer: GPUBuffer | null = null;
	let uniformBindGroup: GPUBindGroup | null = null;
	let posBuffer: GPUBuffer | null = null;
	let colorBuffer: GPUBuffer | null = null;
	let stateBuffer: GPUBuffer | null = null;
	// Allocated byte capacity of each vertex buffer. We reuse the buffer across
	// recolours/repositions (writeBuffer only) and reallocate it ONLY when the
	// data grows past what we've allocated. The hot path (a filterAlpha slider
	// tick) now touches NO vertex buffer at all — it writes a 4-byte uniform —
	// and a selection change re-uploads only the n-byte state buffer.
	let posCapacity = 0;
	let colorCapacity = 0;
	let stateCapacity = 0;
	let count = 0;
	let ready = false; // device + pipeline initialised

	// identity tracking so we re-upload only when a prop's buffer actually swaps
	let lastXBits: Uint16Array | null = null;
	let lastBaseRgb: Uint8Array | null = null;
	let lastState: Uint8Array | null = null;

	// ── clear colour parsed from the background hex (0..1, opaque) ────────────
	function clearRgb(hex: string): GPUColor {
		let h = hex.trim().replace('#', '');
		if (h.length === 3) h = h[0]! + h[0]! + h[1]! + h[1]! + h[2]! + h[2]!;
		const n = Number.parseInt(h, 16);
		if (!Number.isFinite(n)) return { r: 0, g: 0, b: 0, a: 1 };
		return { r: ((n >> 16) & 255) / 255, g: ((n >> 8) & 255) / 255, b: (n & 255) / 255, a: 1 };
	}

	const WGSL = `
struct U {
  center : vec2f,   // data coords shown at canvas centre
  scale  : f32,     // device px per data unit
  pointSize : f32,  // point diameter, device px
  resolution : vec2f, // device px
  filterAlpha : f32,  // 0..1 opacity for DIMMED points (slider)
  noiseAlpha : f32,   // 0..1 opacity for NOISE points
};
@group(0) @binding(0) var<uniform> u : U;

struct VSOut {
  @builtin(position) pos : vec4f,
  @location(0) rgb   : vec3f,   // per-point category hue
  @location(1) quad  : vec2f,   // [-0.5,0.5] local, for the round mask
  @location(2) @interpolate(flat) state : u32, // membership enum
};

@vertex
fn vs(@builtin(vertex_index) vi : u32,
      @location(0) iPos : vec2f,
      @location(1) iColor : vec4f,
      @location(2) iState : vec4u) -> VSOut {
  var C = array<vec2f,6>(
    vec2f(-0.5,-0.5), vec2f(0.5,-0.5), vec2f(-0.5,0.5),
    vec2f(-0.5,0.5),  vec2f(0.5,-0.5), vec2f(0.5,0.5));
  let corner = C[vi];
  let screen = (iPos - u.center) * u.scale + u.resolution * 0.5;
  let p = screen + corner * u.pointSize;
  let clip = (p / u.resolution) * 2.0 - vec2f(1.0, 1.0);
  var o : VSOut;
  o.pos = vec4f(clip.x, -clip.y, 0.0, 1.0);  // flip Y (screen is y-down)
  o.rgb = iColor.rgb;
  o.quad = corner;
  o.state = iState.x;
  return o;
}

@fragment
fn fs(@location(0) rgb : vec3f, @location(1) quad : vec2f,
      @location(2) @interpolate(flat) state : u32) -> @location(0) vec4f {
  // alpha per membership state — reproduces AtlasMap's old alphaFor() exactly.
  // Priority: HIDDEN > DIMMED (filtered-out / not-selected) > NOISE > NORMAL.
  if (state == 3u) { discard; }                 // HIDDEN
  if (dot(quad, quad) > 0.25) { discard; }      // round point
  var a : f32 = 1.0;                            // NORMAL
  if (state == 2u) { a = u.filterAlpha; }       // DIMMED
  else if (state == 1u) { a = u.noiseAlpha; }   // NOISE
  return vec4f(rgb * a, a);                      // premultiplied (a=0 → invisible no-op)
}
`;

	type GpuHandles = {
		device: GPUDevice;
		ctx: GPUCanvasContext;
		pipeline: GPURenderPipeline;
		uniformBuffer: GPUBuffer;
		uniformBindGroup: GPUBindGroup;
	};

	// Returns the freshly-created handles WITHOUT writing the shared refs. The
	// caller assigns them only if its effect wasn't torn down mid-await; otherwise
	// it destroys the device — so an unmount during requestAdapter/requestDevice
	// can't orphan a live GPUDevice that nothing destroys.
	async function initGpu(canvas: HTMLCanvasElement): Promise<GpuHandles | null> {
		if (!navigator.gpu) {
			gpuError = 'WebGPU is required (not available in this browser)';
			return null;
		}
		let adapter: GPUAdapter | null = null;
		try {
			adapter = await navigator.gpu.requestAdapter();
		} catch {
			adapter = null;
		}
		if (!adapter) {
			gpuError = 'WebGPU is required (not available in this browser)';
			return null;
		}
		let dev: GPUDevice;
		try {
			dev = await adapter.requestDevice();
		} catch {
			gpuError = 'WebGPU is required (not available in this browser)';
			return null;
		}
		const context = canvas.getContext('webgpu');
		if (!context) {
			gpuError = 'WebGPU is required (not available in this browser)';
			return null;
		}
		const fmt = navigator.gpu.getPreferredCanvasFormat();
		context.configure({ device: dev, format: fmt, alphaMode: 'premultiplied' });

		const module = dev.createShaderModule({ code: WGSL });
		const pl = dev.createRenderPipeline({
			layout: 'auto',
			vertex: {
				module,
				entryPoint: 'vs',
				buffers: [
					{
						// position: float16x2 (4 bytes/point, was float32x2/8). The shader's
						// `iPos: vec2f` auto-converts f16→f32.
						arrayStride: 4,
						stepMode: 'instance',
						attributes: [{ shaderLocation: 0, offset: 0, format: 'float16x2' }],
					},
					{
						// base colour: per-point category RGB (alpha byte unused). The shader
						// reads `.rgb` and applies the state-derived alpha itself.
						arrayStride: 4,
						stepMode: 'instance',
						attributes: [{ shaderLocation: 1, offset: 0, format: 'unorm8x4' }],
					},
					{
						// state: membership enum in `.x` (uint8x4 — WebGPU requires
						// arrayStride%4==0, so the source byte is padded to 4 at upload).
						arrayStride: 4,
						stepMode: 'instance',
						attributes: [{ shaderLocation: 2, offset: 0, format: 'uint8x4' }],
					},
				],
			},
			fragment: {
				module,
				entryPoint: 'fs',
				targets: [
					{
						format: fmt,
						blend: {
							color: { srcFactor: 'one', dstFactor: 'one-minus-src-alpha', operation: 'add' },
							alpha: { srcFactor: 'one', dstFactor: 'one-minus-src-alpha', operation: 'add' },
						},
					},
				],
			},
			primitive: { topology: 'triangle-list' },
		});

		const ubo = dev.createBuffer({
			// center vec2f(8) + scale f32(4) + pointSize f32(4) + resolution vec2f(8,
			// @16) + filterAlpha f32(4, @24) + noiseAlpha f32(4, @28) = 32 bytes
			// (already a 16-byte multiple).
			size: 32,
			usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
		});
		const bindGroup = dev.createBindGroup({
			layout: pl.getBindGroupLayout(0),
			entries: [{ binding: 0, resource: { buffer: ubo } }],
		});

		return {
			device: dev,
			ctx: context,
			pipeline: pl,
			uniformBuffer: ubo,
			uniformBindGroup: bindGroup,
		};
	}

	// ── data → GPU buffers ────────────────────────────────────────────────────
	/** Interleave the raw f16 x/y bits into ONE Uint16Array(n*2) — the GPU reads
	 *  it as float16x2 (4 bytes/point, half the old float32x2 buffer). */
	function uploadPositions(): void {
		const dev = device;
		if (!dev) return;
		const n = Math.min(xBits.length, yBits.length);
		count = n;
		const interleaved = new Uint16Array(n * 2);
		for (let i = 0; i < n; i++) {
			interleaved[i * 2] = xBits[i]!;
			interleaved[i * 2 + 1] = yBits[i]!;
		}
		const byteLength = Math.max(interleaved.byteLength, 4); // GPU buffers must be non-empty
		// Reuse the existing buffer when it's large enough; only (re)create on grow.
		if (!posBuffer || byteLength > posCapacity) {
			if (posBuffer) posBuffer.destroy();
			posBuffer = dev.createBuffer({
				size: byteLength,
				usage: GPUBufferUsage.VERTEX | GPUBufferUsage.COPY_DST,
			});
			posCapacity = byteLength;
		}
		dev.queue.writeBuffer(
			posBuffer,
			0,
			interleaved.buffer,
			interleaved.byteOffset,
			interleaved.byteLength,
		);
	}

	/** Upload the per-point base RGB (the category hue). Always 4 bytes/point on
	 *  the GPU (unorm8x4 reads .rgb; the alpha byte is unused). Accepts a packed
	 *  3-byte/point source and expands, or a 4-byte/point source uploaded as-is.
	 *  Rebuilt rarely (colorBy / theme change), so the expand cost is negligible. */
	function uploadBase(): void {
		const dev = device;
		if (!dev) return;
		const n = count;
		let rgba4: Uint8Array;
		if (baseRgb.length >= n * 4) {
			rgba4 = baseRgb;
		} else {
			rgba4 = new Uint8Array(n * 4);
			for (let i = 0; i < n; i++) {
				rgba4[i * 4] = baseRgb[i * 3] ?? 0;
				rgba4[i * 4 + 1] = baseRgb[i * 3 + 1] ?? 0;
				rgba4[i * 4 + 2] = baseRgb[i * 3 + 2] ?? 0;
			}
		}
		const byteLength = Math.max(rgba4.byteLength, 4); // GPU buffers must be non-empty
		if (!colorBuffer || byteLength > colorCapacity) {
			if (colorBuffer) colorBuffer.destroy();
			colorBuffer = dev.createBuffer({
				size: byteLength,
				usage: GPUBufferUsage.VERTEX | GPUBufferUsage.COPY_DST,
			});
			colorCapacity = byteLength;
		}
		dev.queue.writeBuffer(colorBuffer, 0, rgba4.buffer, rgba4.byteOffset, rgba4.byteLength);
	}

	/** Upload the per-point state enum. The source is n bytes; the GPU attribute
	 *  is uint8x4 (arrayStride must be %4==0, so stride 1 is INVALID) — expand to
	 *  Uint8Array(n*4) with the enum in `.x` and the rest zero. n bytes of state
	 *  re-uploaded on a selection/filter change is 8× smaller than the old RGBA. */
	function uploadState(): void {
		const dev = device;
		if (!dev) return;
		const n = count;
		const padded = new Uint8Array(n * 4);
		for (let i = 0; i < n; i++) padded[i * 4] = pointState[i] ?? 0;
		const byteLength = Math.max(padded.byteLength, 4); // GPU buffers must be non-empty
		if (!stateBuffer || byteLength > stateCapacity) {
			if (stateBuffer) stateBuffer.destroy();
			stateBuffer = dev.createBuffer({
				size: byteLength,
				usage: GPUBufferUsage.VERTEX | GPUBufferUsage.COPY_DST,
			});
			stateCapacity = byteLength;
		}
		dev.queue.writeBuffer(stateBuffer, 0, padded.buffer, padded.byteOffset, padded.byteLength);
	}

	/** Fit the camera so the whole point cloud is visible & centred. The data
	 *  extent is computed by AtlasMap from the SAME decoded f32 x/y the grid uses
	 *  (we only hold the raw f16 bits here), so the initial fit matches the
	 *  rendered f16 positions. */
	function fit(): void {
		let { minX, minY, maxX, maxY } = fitBounds;
		if (!Number.isFinite(minX)) {
			minX = 0;
			minY = 0;
			maxX = 1;
			maxY = 1;
		}
		const spanX = maxX - minX || 1;
		const spanY = maxY - minY || 1;
		const w = Math.max(1, width);
		const h = Math.max(1, height);
		scale = 0.9 * Math.min((w * dpr) / spanX, (h * dpr) / spanY);
		center = [(minX + maxX) / 2, (minY + maxY) / 2];
	}

	// ── coalesced redraw (one rAF, never a synchronous loop) ──────────────────
	let drawRaf = 0;
	function requestDraw(): void {
		if (drawRaf !== 0) return;
		drawRaf = requestAnimationFrame(() => {
			drawRaf = 0;
			draw();
		});
	}

	function draw(): void {
		const dev = device;
		const context = ctx;
		const canvas = gpuCanvas;
		if (!dev || !context || !canvas || !pipeline || !uniformBuffer || !uniformBindGroup) return;
		const resW = Math.max(1, Math.round(width * dpr));
		const resH = Math.max(1, Math.round(height * dpr));
		if (canvas.width !== resW) canvas.width = resW;
		if (canvas.height !== resH) canvas.height = resH;

		// uniforms: center vec2f, scale f32, pointSize f32, resolution vec2f,
		// filterAlpha f32, noiseAlpha f32 (byte alphas → 0..1 here). A slider tick
		// changes only u[6] and is picked up on the next draw — no buffer re-upload.
		const u = new Float32Array(8);
		u[0] = center[0];
		u[1] = center[1];
		u[2] = scale;
		u[3] = Math.max(1.5, pointSize);
		u[4] = resW;
		u[5] = resH;
		u[6] = filterAlpha / 255;
		u[7] = noiseAlpha / 255;
		dev.queue.writeBuffer(uniformBuffer, 0, u.buffer, u.byteOffset, u.byteLength);

		const enc = dev.createCommandEncoder();
		const pass = enc.beginRenderPass({
			colorAttachments: [
				{
					view: context.getCurrentTexture().createView(),
					clearValue: clearRgb(background),
					loadOp: 'clear',
					storeOp: 'store',
				},
			],
		});
		let drawn = 0;
		if (count > 0 && posBuffer && colorBuffer && stateBuffer) {
			pass.setPipeline(pipeline);
			pass.setBindGroup(0, uniformBindGroup);
			pass.setVertexBuffer(0, posBuffer);
			pass.setVertexBuffer(1, colorBuffer);
			pass.setVertexBuffer(2, stateBuffer);
			pass.draw(6, count);
			drawn = count;
		}
		pass.end();
		dev.queue.submit([enc.finish()]);
		// Test hook: expose how many instanced points the last frame drew, so an
		// e2e check can assert the WebGPU scatter actually rendered (pointsDrawn > 0).
		if (typeof window !== 'undefined') {
			(window as unknown as { __atlasStats?: { pointsDrawn: number } }).__atlasStats = {
				pointsDrawn: drawn,
			};
		}
	}

	// ── coord helpers ─────────────────────────────────────────────────────────
	/** CSS-px (relative to canvas) → data coords. */
	function screenToData(cssX: number, cssY: number): [number, number] {
		const resW = width * dpr;
		const resH = height * dpr;
		const dx = (cssX * dpr - resW * 0.5) / scale + center[0];
		const dy = (cssY * dpr - resH * 0.5) / scale + center[1];
		return [dx, dy];
	}

	// ── lifecycle: init once the canvas mounts ────────────────────────────────
	$effect(() => {
		const canvas = gpuCanvas;
		if (!canvas) return;
		let cancelled = false;
		(async () => {
			const h = await initGpu(canvas);
			if (!h) return; // init failed — gpuError is set, nothing to clean up
			if (cancelled) {
				// Torn down while requestAdapter/requestDevice was still pending:
				// destroy the just-created device rather than orphaning it (the
				// cleanup below already ran with the refs still null).
				h.device.destroy();
				return;
			}
			device = h.device;
			ctx = h.ctx;
			pipeline = h.pipeline;
			uniformBuffer = h.uniformBuffer;
			uniformBindGroup = h.uniformBindGroup;
			ready = true;
			uploadPositions();
			uploadBase();
			uploadState();
			lastXBits = xBits;
			lastBaseRgb = baseRgb;
			lastState = pointState;
			fit();
			requestDraw();
		})();
		return () => {
			cancelled = true;
			if (drawRaf !== 0) {
				cancelAnimationFrame(drawRaf);
				drawRaf = 0;
			}
			if (posBuffer) posBuffer.destroy();
			if (colorBuffer) colorBuffer.destroy();
			if (stateBuffer) stateBuffer.destroy();
			if (uniformBuffer) uniformBuffer.destroy();
			if (device) device.destroy();
			device = null;
			ctx = null;
			pipeline = null;
			uniformBuffer = null;
			uniformBindGroup = null;
			posBuffer = null;
			colorBuffer = null;
			stateBuffer = null;
			posCapacity = 0;
			colorCapacity = 0;
			stateCapacity = 0;
			ready = false;
		};
	});

	// Re-upload + refit when the position bits swap identity (space change / new
	// data). Identity keys on xBits (x and y always swap together on a load).
	$effect(() => {
		void xBits;
		void yBits;
		if (!ready || xBits === lastXBits) return;
		uploadPositions();
		lastXBits = xBits;
		fit();
		requestDraw();
	});

	// Re-upload the base hue only when it swaps identity (colorBy / theme change).
	$effect(() => {
		void baseRgb;
		if (!ready || baseRgb === lastBaseRgb) return;
		uploadBase();
		lastBaseRgb = baseRgb;
		requestDraw();
	});

	// Re-upload the membership state only when it swaps identity (filter /
	// selection / hidden change) — n bytes, 8× smaller than the old RGBA buffer.
	$effect(() => {
		void pointState;
		if (!ready || pointState === lastState) return;
		uploadState();
		lastState = pointState;
		requestDraw();
	});

	// A filterAlpha (or noiseAlpha) slider tick: draw() reads it into the uniform
	// every frame, so just request a redraw — ZERO 145k JS, ZERO buffer upload.
	$effect(() => {
		void filterAlpha;
		void noiseAlpha;
		if (ready) requestDraw();
	});

	// Redraw on any camera / size / point-size change.
	$effect(() => {
		void center;
		void scale;
		void width;
		void height;
		void pointSize;
		void background;
		if (ready) requestDraw();
	});

	// ── pointer gestures ──────────────────────────────────────────────────────
	type Drag = {
		kind: 'pan' | 'lasso';
		startX: number; // css px
		startY: number;
		lastX: number;
		lastY: number;
		moved: boolean;
		lassoPts: [number, number][]; // css px vertices
	};
	let drag: Drag | null = null;
	const CLICK_SLOP = 4; // css px; below this a drag counts as a click

	// rAF-throttled hover (never write reactive state per raw pointer event)
	let hoverRaf = 0;
	let pendingHover: [number, number] | null = null;
	function flushHover(): void {
		hoverRaf = 0;
		if (pendingHover) onHover(pendingHover[0], pendingHover[1]);
	}

	function localCss(e: PointerEvent | WheelEvent): [number, number] {
		const rect = gpuCanvas?.getBoundingClientRect();
		if (!rect) return [0, 0];
		return [e.clientX - rect.left, e.clientY - rect.top];
	}

	function onPointerDown(e: PointerEvent): void {
		const [cx, cy] = localCss(e);
		gpuCanvas?.setPointerCapture(e.pointerId);
		// Pan when: middle button, OR shift+left, OR pan-mode left.
		const wantPan = e.button === 1 || (e.button === 0 && (e.shiftKey || mode === 'pan'));
		drag = {
			kind: wantPan ? 'pan' : 'lasso',
			startX: cx,
			startY: cy,
			lastX: cx,
			lastY: cy,
			moved: false,
			lassoPts: wantPan ? [] : [[cx, cy]],
		};
	}

	function onPointerMove(e: PointerEvent): void {
		const [cx, cy] = localCss(e);
		if (drag) {
			const dxCss = cx - drag.lastX;
			const dyCss = cy - drag.lastY;
			drag.lastX = cx;
			drag.lastY = cy;
			if (Math.abs(cx - drag.startX) > CLICK_SLOP || Math.abs(cy - drag.startY) > CLICK_SLOP) {
				drag.moved = true;
			}
			if (drag.kind === 'pan') {
				// center -= deltaDevicePx / scale
				center = [center[0] - (dxCss * dpr) / scale, center[1] - (dyCss * dpr) / scale];
			} else {
				drag.lassoPts.push([cx, cy]);
				drawLassoOverlay();
			}
			return;
		}
		// plain hover (rAF-coalesced)
		const [dx, dy] = screenToData(cx, cy);
		pendingHover = [dx, dy];
		if (hoverRaf === 0) hoverRaf = requestAnimationFrame(flushHover);
	}

	function onPointerUp(e: PointerEvent): void {
		const d = drag;
		drag = null;
		if (gpuCanvas?.hasPointerCapture(e.pointerId)) gpuCanvas.releasePointerCapture(e.pointerId);
		clearLassoOverlay();
		if (!d) return;
		const [cx, cy] = localCss(e);
		if (!d.moved) {
			// a click → pick at data coords
			const [dx, dy] = screenToData(cx, cy);
			onPick(dx, dy);
			return;
		}
		if (d.kind === 'lasso' && d.lassoPts.length >= 3) {
			const flat: number[] = [];
			for (const [px, py] of d.lassoPts) {
				const [dx, dy] = screenToData(px, py);
				flat.push(dx, dy);
			}
			onLasso(flat);
		}
	}

	function onPointerLeave(): void {
		if (hoverRaf !== 0) {
			cancelAnimationFrame(hoverRaf);
			hoverRaf = 0;
		}
		pendingHover = null;
		onHoverEnd();
	}

	// OS-interrupted gesture (incoming call, browser takeover): abandon the drag
	// WITHOUT committing a pick or lasso — a cancel is not an up.
	function onPointerCancel(): void {
		drag = null;
		clearLassoOverlay();
	}

	function onWheel(e: WheelEvent): void {
		e.preventDefault();
		const [cx, cy] = localCss(e);
		// data point under the cursor before zoom
		const [dx, dy] = screenToData(cx, cy);
		const next = Math.max(1e-4, Math.min(1e7, scale * Math.exp(-e.deltaY * 0.001)));
		// adjust center so (dx,dy) stays under the cursor after the scale change
		const resW = width * dpr;
		const resH = height * dpr;
		const cxDev = cx * dpr - resW * 0.5;
		const cyDev = cy * dpr - resH * 0.5;
		scale = next;
		center = [dx - cxDev / next, dy - cyDev / next];
	}

	// ── lasso overlay (2D canvas, kept off the GPU surface) ───────────────────
	function drawLassoOverlay(): void {
		const c = overlayCanvas;
		const d = drag;
		if (!c || !d || d.kind !== 'lasso') return;
		if (c.width !== Math.round(width * dpr)) c.width = Math.round(width * dpr);
		if (c.height !== Math.round(height * dpr)) c.height = Math.round(height * dpr);
		const ctx2d = c.getContext('2d');
		if (!ctx2d) return;
		ctx2d.clearRect(0, 0, c.width, c.height);
		if (d.lassoPts.length < 2) return;
		ctx2d.save();
		ctx2d.scale(dpr, dpr);
		ctx2d.lineWidth = 1.5;
		ctx2d.strokeStyle = 'rgba(99,102,241,0.9)';
		ctx2d.fillStyle = 'rgba(99,102,241,0.12)';
		ctx2d.beginPath();
		const first = d.lassoPts[0]!;
		ctx2d.moveTo(first[0], first[1]);
		for (let i = 1; i < d.lassoPts.length; i++) ctx2d.lineTo(d.lassoPts[i]![0], d.lassoPts[i]![1]);
		ctx2d.closePath();
		ctx2d.fill();
		ctx2d.stroke();
		ctx2d.restore();
	}

	function clearLassoOverlay(): void {
		const c = overlayCanvas;
		if (!c) return;
		const ctx2d = c.getContext('2d');
		ctx2d?.clearRect(0, 0, c.width, c.height);
	}

	$effect(() => () => {
		if (hoverRaf !== 0) cancelAnimationFrame(hoverRaf);
	});

	const cursor = $derived(mode === 'pan' ? 'grab' : 'crosshair');

	// Screen position (css px) of the active-point highlight ring, recomputed as
	// the camera pans/zooms (reads center/scale — the same transform `screenToData`
	// inverts). Hidden when the point is off-screen.
	const markerScreen = $derived.by((): { left: number; top: number } | null => {
		if (!markerXY) return null;
		const left = ((markerXY[0] - center[0]) * scale) / dpr + width / 2;
		const top = ((markerXY[1] - center[1]) * scale) / dpr + height / 2;
		if (left < -8 || left > width + 8 || top < -8 || top > height + 8) return null;
		return { left, top };
	});
</script>

{#if gpuError}
	<div class="text-destructive grid h-full place-items-center p-6 text-center text-sm">
		{gpuError} — the embedding map needs WebGPU.
	</div>
{:else}
	<canvas
		bind:this={gpuCanvas}
		class="absolute inset-0 block touch-none"
		style:width="{width}px"
		style:height="{height}px"
		style:cursor
		onpointerdown={onPointerDown}
		onpointermove={onPointerMove}
		onpointerup={onPointerUp}
		onpointerleave={onPointerLeave}
		onpointercancel={onPointerCancel}
		onwheel={onWheel}
	></canvas>
	<canvas
		bind:this={overlayCanvas}
		class="pointer-events-none absolute inset-0 block"
		style:width="{width}px"
		style:height="{height}px"
	></canvas>
	<!-- subtle dot grid backing the embedding space (theme-neutral grey) -->
	<div
		class="pointer-events-none absolute inset-0"
		style:background-image="radial-gradient(rgba(130,130,130,0.14) 1px, transparent 1.5px)"
		style:background-size="22px 22px"
	></div>
	{#if markerScreen}
		<!-- highlight ring for the active hit (the point shown in the player / table) -->
		<div
			class="border-primary pointer-events-none absolute z-10 size-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2"
			style:left="{markerScreen.left}px"
			style:top="{markerScreen.top}px"
			style:box-shadow="0 0 0 2px rgba(0,0,0,0.45), 0 0 10px 2px var(--color-primary)"
		></div>
	{/if}
{/if}
