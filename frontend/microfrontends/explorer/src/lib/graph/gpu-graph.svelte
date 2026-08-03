<script lang="ts">
	/**
	 * gpu-graph — a self-contained WebGPU node-link renderer for the KG explorer.
	 *
	 * A sibling of atlas/gpu-scatter: same device/context init, data-space pan +
	 * cursor-anchored wheel zoom, coalesced rAF redraw, and instanced-quad node
	 * sprites. Two differences make it a graph renderer:
	 *   1. positions arrive as plain Float32Array (node counts are small, ≤ ~2000),
	 *      and they MUTATE IN PLACE during the d3-force ticks — so the parent bumps
	 *      `layoutVersion` each tick as the re-upload signal (buffer identity is
	 *      stable; only the contents change);
	 *   2. an EDGE pipeline (instanced thin quads, one instance per edge) is drawn
	 *      BENEATH the nodes — each instance expands the A→B segment into a 2 px
	 *      ribbon in screen space.
	 *
	 * Hover/pick are a CPU nearest-node search (cheap at this scale, no grid):
	 *   • onHover(index | null, clientX, clientY) — rAF-throttled
	 *   • onPick(index | null)                    — a drag-free click
	 * Hidden nodes (state 3) are skipped in both render and picking.
	 */

	type FitBounds = { minX: number; minY: number; maxX: number; maxY: number };

	let {
		nodesX,
		nodesY,
		layoutVersion,
		nodeRgb,
		nodeSize,
		nodeState,
		edges,
		edgeAlpha = 90,
		fitBounds,
		width,
		height,
		background,
		onHover,
		onPick,
		markerIndex = null,
	}: {
		nodesX: Float32Array; // data-space X per node
		nodesY: Float32Array; // data-space Y per node
		layoutVersion: number; // bump to re-upload positions (they mutate in place)
		nodeRgb: Uint8Array; // 3 bytes/node, category hue
		nodeSize: Float32Array; // device-px diameter per node
		nodeState: Uint8Array; // 0 normal / 2 dimmed / 3 hidden
		edges: Uint32Array; // flat node-index pairs [a0,b0,a1,b1,...]
		edgeAlpha?: number; // 0..255
		fitBounds: FitBounds; // data extent; swap identity to refit
		width: number; // css px
		height: number; // css px
		background: string; // css hex
		onHover?: (index: number | null, clientX: number, clientY: number) => void;
		onPick?: (index: number | null) => void;
		markerIndex?: number | null; // node index to ring-highlight
	} = $props();

	const dpr = Math.min(globalThis.devicePixelRatio ?? 1, 1.5);
	const PICK_RADIUS = 14; // css px — nearest-node hit radius
	const DIMMED_ALPHA = 0.16;
	const EDGE_RGB: [number, number, number] = [0.55, 0.58, 0.64]; // slate

	let gpuCanvas = $state<HTMLCanvasElement | null>(null);
	let gpuError = $state<string | null>(null);

	// camera (data coords at canvas centre + device-px per data unit)
	let center = $state.raw<[number, number]>([0, 0]);
	let scale = $state(1);
	// a $state counter the marker derivation depends on so the ring tracks ticks
	let posTick = $state(0);

	// GPU handles (plain refs; not reactive)
	let device: GPUDevice | null = null;
	let ctx: GPUCanvasContext | null = null;
	let nodePipeline: GPURenderPipeline | null = null;
	let edgePipeline: GPURenderPipeline | null = null;
	let uniformBuffer: GPUBuffer | null = null;
	let nodeBindGroup: GPUBindGroup | null = null;
	let edgeBindGroup: GPUBindGroup | null = null;
	let posBuffer: GPUBuffer | null = null;
	let sizeBuffer: GPUBuffer | null = null;
	let colorBuffer: GPUBuffer | null = null;
	let stateBuffer: GPUBuffer | null = null;
	let edgeBuffer: GPUBuffer | null = null;
	let posCapacity = 0;
	let sizeCapacity = 0;
	let colorCapacity = 0;
	let stateCapacity = 0;
	let edgeCapacity = 0;
	let nodeCount = 0;
	let edgeCount = 0;
	let ready = false;

	let lastLayout = -1;
	let lastEdges: Uint32Array | null = null;
	let lastRgb: Uint8Array | null = null;
	let lastSize: Float32Array | null = null;
	let lastState: Uint8Array | null = null;
	let lastFit: FitBounds | null = null;

	function clearRgb(hex: string): GPUColor {
		let h = hex.trim().replace('#', '');
		if (h.length === 3) h = h[0]! + h[0]! + h[1]! + h[1]! + h[2]! + h[2]!;
		const n = Number.parseInt(h, 16);
		if (!Number.isFinite(n)) return { r: 0, g: 0, b: 0, a: 1 };
		return { r: ((n >> 16) & 255) / 255, g: ((n >> 8) & 255) / 255, b: (n & 255) / 255, a: 1 };
	}

	// One module, one shared uniform; two pipelines (edges drawn first, beneath).
	const WGSL = `
struct U {
  center : vec2f,      // 0
  scale : f32,         // 8
  lineWidth : f32,     // 12 (device px)
  resolution : vec2f,  // 16
  edgeAlpha : f32,     // 24 (0..1)
  dimmed : f32,        // 28 (0..1)
  edgeRgb : vec3f,     // 32
};
@group(0) @binding(0) var<uniform> u : U;

fn toClip(screen : vec2f) -> vec4f {
  let clip = (screen / u.resolution) * 2.0 - vec2f(1.0, 1.0);
  return vec4f(clip.x, -clip.y, 0.0, 1.0);   // flip Y (screen is y-down)
}

// ── edges: one instance per edge, a unit quad expanded into a screen ribbon ──
@vertex
fn vsEdge(@builtin(vertex_index) vi : u32, @location(0) seg : vec4f) -> @builtin(position) vec4f {
  var T = array<f32,6>(0.0, 1.0, 0.0, 0.0, 1.0, 1.0);
  var S = array<f32,6>(-0.5, -0.5, 0.5, 0.5, -0.5, 0.5);
  let a = (seg.xy - u.center) * u.scale + u.resolution * 0.5;
  let b = (seg.zw - u.center) * u.scale + u.resolution * 0.5;
  var dir = b - a;
  let len = max(length(dir), 1e-4);
  dir = dir / len;
  let perp = vec2f(-dir.y, dir.x);
  let p = mix(a, b, T[vi]) + perp * (S[vi] * u.lineWidth);
  return toClip(p);
}

@fragment
fn fsEdge() -> @location(0) vec4f {
  return vec4f(u.edgeRgb * u.edgeAlpha, u.edgeAlpha);  // premultiplied
}

// ── nodes: instanced round sprites, per-node size + colour + state ───────────
struct VSOut {
  @builtin(position) pos : vec4f,
  @location(0) rgb : vec3f,
  @location(1) quad : vec2f,
  @location(2) @interpolate(flat) state : u32,
};

@vertex
fn vsNode(@builtin(vertex_index) vi : u32,
          @location(0) iPos : vec2f,
          @location(1) iSize : f32,
          @location(2) iColor : vec4f,
          @location(3) iState : vec4u) -> VSOut {
  var C = array<vec2f,6>(
    vec2f(-0.5,-0.5), vec2f(0.5,-0.5), vec2f(-0.5,0.5),
    vec2f(-0.5,0.5),  vec2f(0.5,-0.5), vec2f(0.5,0.5));
  let corner = C[vi];
  let screen = (iPos - u.center) * u.scale + u.resolution * 0.5;
  let p = screen + corner * iSize;
  var o : VSOut;
  o.pos = toClip(p);
  o.rgb = iColor.rgb;
  o.quad = corner;
  o.state = iState.x;
  return o;
}

@fragment
fn fsNode(@location(0) rgb : vec3f, @location(1) quad : vec2f,
          @location(2) @interpolate(flat) state : u32) -> @location(0) vec4f {
  if (state == 3u) { discard; }              // HIDDEN
  if (dot(quad, quad) > 0.25) { discard; }   // round mask
  var a : f32 = 1.0;
  if (state == 2u) { a = u.dimmed; }         // DIMMED
  return vec4f(rgb * a, a);                   // premultiplied
}
`;

	type GpuHandles = {
		device: GPUDevice;
		ctx: GPUCanvasContext;
		nodePipeline: GPURenderPipeline;
		edgePipeline: GPURenderPipeline;
		uniformBuffer: GPUBuffer;
		nodeBindGroup: GPUBindGroup;
		edgeBindGroup: GPUBindGroup;
	};

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
		const blend = {
			color: { srcFactor: 'one', dstFactor: 'one-minus-src-alpha', operation: 'add' },
			alpha: { srcFactor: 'one', dstFactor: 'one-minus-src-alpha', operation: 'add' },
		} as const;
		const module = dev.createShaderModule({ code: WGSL });

		const edgePl = dev.createRenderPipeline({
			layout: 'auto',
			vertex: {
				module,
				entryPoint: 'vsEdge',
				buffers: [
					{
						arrayStride: 16, // vec4f: ax, ay, bx, by (data coords)
						stepMode: 'instance',
						attributes: [{ shaderLocation: 0, offset: 0, format: 'float32x4' }],
					},
				],
			},
			fragment: { module, entryPoint: 'fsEdge', targets: [{ format: fmt, blend }] },
			primitive: { topology: 'triangle-list' },
		});

		const nodePl = dev.createRenderPipeline({
			layout: 'auto',
			vertex: {
				module,
				entryPoint: 'vsNode',
				buffers: [
					{
						arrayStride: 8,
						stepMode: 'instance',
						attributes: [{ shaderLocation: 0, offset: 0, format: 'float32x2' }],
					},
					{
						arrayStride: 4,
						stepMode: 'instance',
						attributes: [{ shaderLocation: 1, offset: 0, format: 'float32' }],
					},
					{
						arrayStride: 4,
						stepMode: 'instance',
						attributes: [{ shaderLocation: 2, offset: 0, format: 'unorm8x4' }],
					},
					{
						arrayStride: 4,
						stepMode: 'instance',
						attributes: [{ shaderLocation: 3, offset: 0, format: 'uint8x4' }],
					},
				],
			},
			fragment: { module, entryPoint: 'fsNode', targets: [{ format: fmt, blend }] },
			primitive: { topology: 'triangle-list' },
		});

		const ubo = dev.createBuffer({
			size: 48,
			usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
		});
		const mk = (pl: GPURenderPipeline) =>
			dev.createBindGroup({
				layout: pl.getBindGroupLayout(0),
				entries: [{ binding: 0, resource: { buffer: ubo } }],
			});

		return {
			device: dev,
			ctx: context,
			nodePipeline: nodePl,
			edgePipeline: edgePl,
			uniformBuffer: ubo,
			nodeBindGroup: mk(nodePl),
			edgeBindGroup: mk(edgePl),
		};
	}

	function ensure(
		buf: GPUBuffer | null,
		byteLength: number,
		capacity: number,
	): [GPUBuffer, number] {
		const dev = device!;
		const size = Math.max(byteLength, 4); // GPU buffers must be non-empty
		if (buf && size <= capacity) return [buf, capacity];
		if (buf) buf.destroy();
		return [
			dev.createBuffer({ size, usage: GPUBufferUsage.VERTEX | GPUBufferUsage.COPY_DST }),
			size,
		];
	}

	function uploadPositions(): void {
		const dev = device;
		if (!dev) return;
		const n = Math.min(nodesX.length, nodesY.length);
		nodeCount = n;
		const inter = new Float32Array(n * 2);
		for (let i = 0; i < n; i++) {
			inter[i * 2] = nodesX[i]!;
			inter[i * 2 + 1] = nodesY[i]!;
		}
		[posBuffer, posCapacity] = ensure(posBuffer, inter.byteLength, posCapacity);
		dev.queue.writeBuffer(posBuffer, 0, inter.buffer, inter.byteOffset, inter.byteLength);
	}

	function uploadSize(): void {
		const dev = device;
		if (!dev) return;
		const n = nodeCount;
		const arr = nodeSize.length >= n ? nodeSize.subarray(0, n) : new Float32Array(n).fill(10);
		[sizeBuffer, sizeCapacity] = ensure(sizeBuffer, arr.byteLength, sizeCapacity);
		dev.queue.writeBuffer(sizeBuffer, 0, arr.buffer, arr.byteOffset, arr.byteLength);
	}

	function uploadColor(): void {
		const dev = device;
		if (!dev) return;
		const n = nodeCount;
		const rgba = new Uint8Array(n * 4);
		for (let i = 0; i < n; i++) {
			rgba[i * 4] = nodeRgb[i * 3] ?? 0;
			rgba[i * 4 + 1] = nodeRgb[i * 3 + 1] ?? 0;
			rgba[i * 4 + 2] = nodeRgb[i * 3 + 2] ?? 0;
		}
		[colorBuffer, colorCapacity] = ensure(colorBuffer, rgba.byteLength, colorCapacity);
		dev.queue.writeBuffer(colorBuffer, 0, rgba.buffer, rgba.byteOffset, rgba.byteLength);
	}

	function uploadState(): void {
		const dev = device;
		if (!dev) return;
		const n = nodeCount;
		const padded = new Uint8Array(n * 4);
		for (let i = 0; i < n; i++) padded[i * 4] = nodeState[i] ?? 0;
		[stateBuffer, stateCapacity] = ensure(stateBuffer, padded.byteLength, stateCapacity);
		dev.queue.writeBuffer(stateBuffer, 0, padded.buffer, padded.byteOffset, padded.byteLength);
	}

	function uploadEdges(): void {
		const dev = device;
		if (!dev) return;
		const m = Math.floor(edges.length / 2);
		edgeCount = m;
		const seg = new Float32Array(m * 4);
		for (let e = 0; e < m; e++) {
			const a = edges[e * 2]!;
			const b = edges[e * 2 + 1]!;
			seg[e * 4] = nodesX[a] ?? 0;
			seg[e * 4 + 1] = nodesY[a] ?? 0;
			seg[e * 4 + 2] = nodesX[b] ?? 0;
			seg[e * 4 + 3] = nodesY[b] ?? 0;
		}
		[edgeBuffer, edgeCapacity] = ensure(edgeBuffer, seg.byteLength, edgeCapacity);
		dev.queue.writeBuffer(edgeBuffer, 0, seg.buffer, seg.byteOffset, seg.byteLength);
	}

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
		if (!dev || !context || !canvas || !nodePipeline || !edgePipeline || !uniformBuffer) return;
		const resW = Math.max(1, Math.round(width * dpr));
		const resH = Math.max(1, Math.round(height * dpr));
		if (canvas.width !== resW) canvas.width = resW;
		if (canvas.height !== resH) canvas.height = resH;

		const u = new Float32Array(12); // 48 bytes
		u[0] = center[0];
		u[1] = center[1];
		u[2] = scale;
		u[3] = Math.max(1, 2 * dpr); // lineWidth
		u[4] = resW;
		u[5] = resH;
		u[6] = Math.max(0, Math.min(255, edgeAlpha)) / 255;
		u[7] = DIMMED_ALPHA;
		u[8] = EDGE_RGB[0];
		u[9] = EDGE_RGB[1];
		u[10] = EDGE_RGB[2];
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
		if (edgeCount > 0 && edgeBuffer && edgeBindGroup) {
			pass.setPipeline(edgePipeline);
			pass.setBindGroup(0, edgeBindGroup);
			pass.setVertexBuffer(0, edgeBuffer);
			pass.draw(6, edgeCount);
		}
		if (nodeCount > 0 && posBuffer && sizeBuffer && colorBuffer && stateBuffer && nodeBindGroup) {
			pass.setPipeline(nodePipeline);
			pass.setBindGroup(0, nodeBindGroup);
			pass.setVertexBuffer(0, posBuffer);
			pass.setVertexBuffer(1, sizeBuffer);
			pass.setVertexBuffer(2, colorBuffer);
			pass.setVertexBuffer(3, stateBuffer);
			pass.draw(6, nodeCount);
		}
		pass.end();
		dev.queue.submit([enc.finish()]);
	}

	function screenToData(cssX: number, cssY: number): [number, number] {
		const resW = width * dpr;
		const resH = height * dpr;
		return [
			(cssX * dpr - resW * 0.5) / scale + center[0],
			(cssY * dpr - resH * 0.5) / scale + center[1],
		];
	}

	/** Nearest non-hidden node to a css-px point, within PICK_RADIUS, else null. */
	function nearestNode(cssX: number, cssY: number): number | null {
		let best = -1;
		let bestD = PICK_RADIUS * PICK_RADIUS;
		for (let i = 0; i < nodeCount; i++) {
			if ((nodeState[i] ?? 0) === 3) continue;
			const sx = ((nodesX[i]! - center[0]) * scale) / dpr + width / 2;
			const sy = ((nodesY[i]! - center[1]) * scale) / dpr + height / 2;
			const d = (sx - cssX) * (sx - cssX) + (sy - cssY) * (sy - cssY);
			if (d < bestD) {
				bestD = d;
				best = i;
			}
		}
		return best < 0 ? null : best;
	}

	$effect(() => {
		const canvas = gpuCanvas;
		if (!canvas) return;
		let cancelled = false;
		(async () => {
			const h = await initGpu(canvas);
			if (!h) return;
			if (cancelled) {
				h.device.destroy();
				return;
			}
			device = h.device;
			ctx = h.ctx;
			nodePipeline = h.nodePipeline;
			edgePipeline = h.edgePipeline;
			uniformBuffer = h.uniformBuffer;
			nodeBindGroup = h.nodeBindGroup;
			edgeBindGroup = h.edgeBindGroup;
			ready = true;
			uploadPositions();
			uploadSize();
			uploadColor();
			uploadState();
			uploadEdges();
			lastLayout = layoutVersion;
			lastEdges = edges;
			lastRgb = nodeRgb;
			lastSize = nodeSize;
			lastState = nodeState;
			lastFit = fitBounds;
			fit();
			requestDraw();
		})();
		return () => {
			cancelled = true;
			if (drawRaf !== 0) {
				cancelAnimationFrame(drawRaf);
				drawRaf = 0;
			}
			for (const b of [posBuffer, sizeBuffer, colorBuffer, stateBuffer, edgeBuffer, uniformBuffer])
				b?.destroy();
			if (device) device.destroy();
			device = null;
			ctx = null;
			nodePipeline = null;
			edgePipeline = null;
			uniformBuffer = null;
			posBuffer = sizeBuffer = colorBuffer = stateBuffer = edgeBuffer = null;
			posCapacity = sizeCapacity = colorCapacity = stateCapacity = edgeCapacity = 0;
			ready = false;
		};
	});

	// positions mutate in place every force tick — layoutVersion is the signal.
	$effect(() => {
		void layoutVersion;
		if (!ready || layoutVersion === lastLayout) return;
		lastLayout = layoutVersion;
		uploadPositions();
		uploadEdges(); // endpoints are derived from the (moved) positions
		posTick++;
		requestDraw();
	});

	// edge topology changed (new subgraph) — rebuild from current positions.
	$effect(() => {
		void edges;
		if (!ready || edges === lastEdges) return;
		lastEdges = edges;
		uploadEdges();
		requestDraw();
	});

	$effect(() => {
		void nodeRgb;
		if (!ready || nodeRgb === lastRgb) return;
		lastRgb = nodeRgb;
		uploadColor();
		requestDraw();
	});

	$effect(() => {
		void nodeSize;
		if (!ready || nodeSize === lastSize) return;
		lastSize = nodeSize;
		uploadSize();
		requestDraw();
	});

	$effect(() => {
		void nodeState;
		if (!ready || nodeState === lastState) return;
		lastState = nodeState;
		uploadState();
		requestDraw();
	});

	// parent swaps the fitBounds object to request a refit (Fit button / settle).
	$effect(() => {
		void fitBounds;
		if (!ready || fitBounds === lastFit) return;
		lastFit = fitBounds;
		fit();
		requestDraw();
	});

	$effect(() => {
		void center;
		void scale;
		void width;
		void height;
		void edgeAlpha;
		void background;
		if (ready) requestDraw();
	});

	// ── pointer gestures: drag pans, drag-free click picks ────────────────────
	type Drag = { startX: number; startY: number; lastX: number; lastY: number; moved: boolean };
	let drag: Drag | null = null;
	const CLICK_SLOP = 4;

	let hoverRaf = 0;
	let pendingHover: { idx: number | null; cx: number; cy: number } | null = null;
	function flushHover(): void {
		hoverRaf = 0;
		if (pendingHover) onHover?.(pendingHover.idx, pendingHover.cx, pendingHover.cy);
	}

	function localCss(e: PointerEvent | WheelEvent): [number, number] {
		const rect = gpuCanvas?.getBoundingClientRect();
		if (!rect) return [0, 0];
		return [e.clientX - rect.left, e.clientY - rect.top];
	}

	function onPointerDown(e: PointerEvent): void {
		const [cx, cy] = localCss(e);
		gpuCanvas?.setPointerCapture(e.pointerId);
		drag = { startX: cx, startY: cy, lastX: cx, lastY: cy, moved: false };
	}

	function onPointerMove(e: PointerEvent): void {
		const [cx, cy] = localCss(e);
		if (drag) {
			const dxCss = cx - drag.lastX;
			const dyCss = cy - drag.lastY;
			drag.lastX = cx;
			drag.lastY = cy;
			if (Math.abs(cx - drag.startX) > CLICK_SLOP || Math.abs(cy - drag.startY) > CLICK_SLOP)
				drag.moved = true;
			center = [center[0] - (dxCss * dpr) / scale, center[1] - (dyCss * dpr) / scale];
			return;
		}
		pendingHover = { idx: nearestNode(cx, cy), cx: e.clientX, cy: e.clientY };
		if (hoverRaf === 0) hoverRaf = requestAnimationFrame(flushHover);
	}

	function onPointerUp(e: PointerEvent): void {
		const d = drag;
		drag = null;
		if (gpuCanvas?.hasPointerCapture(e.pointerId)) gpuCanvas.releasePointerCapture(e.pointerId);
		if (!d || d.moved) return;
		const [cx, cy] = localCss(e);
		onPick?.(nearestNode(cx, cy));
	}

	function onPointerLeave(): void {
		if (hoverRaf !== 0) {
			cancelAnimationFrame(hoverRaf);
			hoverRaf = 0;
		}
		pendingHover = null;
		onHover?.(null, 0, 0);
	}

	function onPointerCancel(): void {
		drag = null;
	}

	function onWheel(e: WheelEvent): void {
		e.preventDefault();
		const [cx, cy] = localCss(e);
		const [dx, dy] = screenToData(cx, cy);
		const next = Math.max(1e-4, Math.min(1e7, scale * Math.exp(-e.deltaY * 0.001)));
		const resW = width * dpr;
		const resH = height * dpr;
		scale = next;
		center = [dx - (cx * dpr - resW * 0.5) / next, dy - (cy * dpr - resH * 0.5) / next];
	}

	$effect(() => () => {
		if (hoverRaf !== 0) cancelAnimationFrame(hoverRaf);
	});

	const markerScreen = $derived.by((): { left: number; top: number } | null => {
		void posTick;
		if (markerIndex === null || markerIndex < 0 || markerIndex >= nodeCount) return null;
		const left = ((nodesX[markerIndex]! - center[0]) * scale) / dpr + width / 2;
		const top = ((nodesY[markerIndex]! - center[1]) * scale) / dpr + height / 2;
		if (left < -8 || left > width + 8 || top < -8 || top > height + 8) return null;
		return { left, top };
	});
</script>

{#if gpuError}
	<div class="text-destructive grid h-full place-items-center p-6 text-center text-sm">
		{gpuError} — the graph view needs WebGPU.
	</div>
{:else}
	<canvas
		bind:this={gpuCanvas}
		class="absolute inset-0 block touch-none"
		style:width="{width}px"
		style:height="{height}px"
		style:cursor="grab"
		onpointerdown={onPointerDown}
		onpointermove={onPointerMove}
		onpointerup={onPointerUp}
		onpointerleave={onPointerLeave}
		onpointercancel={onPointerCancel}
		onwheel={onWheel}
	></canvas>
	<div
		class="pointer-events-none absolute inset-0"
		style:background-image="radial-gradient(rgba(130,130,130,0.12) 1px, transparent 1.5px)"
		style:background-size="24px 24px"
	></div>
	{#if markerScreen}
		<div
			class="border-primary pointer-events-none absolute z-10 size-5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2"
			style:left="{markerScreen.left}px"
			style:top="{markerScreen.top}px"
			style:box-shadow="0 0 0 2px rgba(0,0,0,0.45), 0 0 10px 2px var(--color-primary)"
		></div>
	{/if}
{/if}
