import { afterEach, describe, expect, it, vi } from 'vitest';
import { gpuUnsupportedReason, resetGpuSupportProbe } from './gpu-support';

/**
 * This probe decides whether 6.6 MB gets downloaded, so each branch is asserted rather than assumed.
 * Getting it wrong in the permissive direction restores the waste it exists to prevent; getting it wrong in
 * the strict direction hides a working map behind an error.
 */
afterEach(() => {
	resetGpuSupportProbe();
	vi.unstubAllGlobals();
});

/** A navigator whose `gpu.requestAdapter` behaves as the test dictates. */
function withGpu(requestAdapter: () => Promise<unknown>): { calls: () => number } {
	let calls = 0;
	vi.stubGlobal('navigator', {
		gpu: {
			requestAdapter: () => {
				calls += 1;
				return requestAdapter();
			},
		},
	});
	return { calls: () => calls };
}

describe('gpuUnsupportedReason', () => {
	it('no WebGPU at all → a reason, so the projection is never fetched', async () => {
		vi.stubGlobal('navigator', {});
		expect(await gpuUnsupportedReason()).toBe('WebGPU is required (not available in this browser)');
	});

	it('an adapter is granted → null, meaning render normally', async () => {
		withGpu(async () => ({ name: 'swiftshader' }));
		expect(await gpuUnsupportedReason()).toBeNull();
	});

	it('the API exists but no adapter is granted → still unsupported', async () => {
		// A blocklisted driver, a headless run, or a machine with no usable GPU. It renders the same error
		// as no-WebGPU-at-all, so it must gate the same fetch.
		withGpu(async () => null);
		expect(await gpuUnsupportedReason()).toBe('WebGPU is required (no GPU adapter available)');
	});

	it('a throwing requestAdapter is a reason, not an unhandled rejection', async () => {
		withGpu(() => Promise.reject(new Error('driver exploded')));
		expect(await gpuUnsupportedReason()).toBe(
			'WebGPU is required (the GPU adapter request failed)',
		);
	});

	it('probes once and reuses the answer', async () => {
		const gpu = withGpu(async () => ({}));

		const [a, b] = await Promise.all([gpuUnsupportedReason(), gpuUnsupportedReason()]);
		await gpuUnsupportedReason();

		expect(a).toBeNull();
		expect(b).toBeNull();
		expect(gpu.calls()).toBe(1); // support cannot change within a page's life
	});
});
