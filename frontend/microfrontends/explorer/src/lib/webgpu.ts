/**
 * The WebGPU bring-up BOTH canvases share (#96): probe → adapter → device → context → configure.
 * `gpu-scatter.svelte` (the atlas) and `gpu-graph.svelte` (the knowledge graph) carried this
 * verbatim — same steps, same premultiplied alpha mode, same user-facing refusal sentence — and
 * everything AFTER it (shaders, pipelines, bind groups) is genuinely theirs, so this module stops
 * exactly where they diverge.
 *
 * The refusal is a `reason` string rather than a throw for the same reason the callers wrote it
 * that way: an unsupported browser is a rendering OUTCOME (the canvas shows the sentence), not an
 * exception. The pre-download gate stays `gpu-support.ts` — that one exists to refuse BEFORE the
 * projection is fetched; this one hands live handles to a canvas that is already committed.
 */

export type AcquiredGpu = {
	ok: true;
	device: GPUDevice;
	ctx: GPUCanvasContext;
	format: GPUTextureFormat;
};

export async function acquireGpuCanvas(
	canvas: HTMLCanvasElement,
): Promise<AcquiredGpu | { ok: false; reason: string }> {
	const reason = 'WebGPU is required (not available in this browser)';
	if (!navigator.gpu) return { ok: false, reason };
	let adapter: GPUAdapter | null = null;
	try {
		adapter = await navigator.gpu.requestAdapter();
	} catch {
		adapter = null;
	}
	if (!adapter) return { ok: false, reason };
	let device: GPUDevice;
	try {
		device = await adapter.requestDevice();
	} catch {
		return { ok: false, reason };
	}
	const ctx = canvas.getContext('webgpu');
	if (!ctx) return { ok: false, reason };
	const format = navigator.gpu.getPreferredCanvasFormat();
	ctx.configure({ device, format, alphaMode: 'premultiplied' });
	return { ok: true, device, ctx, format };
}
