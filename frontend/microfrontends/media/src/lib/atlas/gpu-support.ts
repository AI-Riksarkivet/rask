/**
 * Can this browser render the atlas at all? Probed once, before anything is downloaded.
 *
 * The atlas map is WebGPU-only by design — the point scatter uses instanced quads with true per-point
 * alpha and there is no legacy fallback. The check for that lived inside `gpu-scatter.svelte`, which is a
 * *child* of `AtlasMap` and only mounts once points have arrived. So the order of events on a browser
 * without WebGPU was: fetch the projection, hand it to the renderer, discover the renderer cannot run, show
 * an error. Measured against the deployed build on 2026-07-26, that cost **6,678,928 bytes per request and
 * 31.8 MiB across four Text/Visual toggles** — every byte of it spent to display a red message.
 *
 * Hoisting the probe here lets the parent decide before it fetches, and keeps one wording in one place for
 * both the gate and the message. The result is memoised because support cannot change within a page's life:
 * a second ask must not open a second adapter request.
 *
 * `requestAdapter()` is included rather than only checking `navigator.gpu`, because a browser can expose the
 * API and still refuse an adapter — a blocklisted driver, a headless run, a machine with no usable GPU. That
 * case rendered the same error, so it must gate the same fetch.
 */

let probe: Promise<string | null> | null = null;

/** `null` when the atlas can render; otherwise the reason, phrased for a user. */
export function gpuUnsupportedReason(): Promise<string | null> {
	probe ??= (async (): Promise<string | null> => {
		if (typeof navigator === 'undefined' || !navigator.gpu) {
			return 'WebGPU is required (not available in this browser)';
		}
		try {
			const adapter = await navigator.gpu.requestAdapter();
			if (!adapter) return 'WebGPU is required (no GPU adapter available)';
		} catch {
			return 'WebGPU is required (the GPU adapter request failed)';
		}
		return null;
	})();
	return probe;
}

/** Testing seam: forget the memoised probe. */
export function resetGpuSupportProbe(): void {
	probe = null;
}
