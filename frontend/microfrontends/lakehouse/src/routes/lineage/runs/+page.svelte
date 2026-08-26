<script lang="ts">
	import { page } from '$app/state';
	import type { RunStatus } from '@rask/api/lineage';
	import { listRuns } from '$lib/api';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';
	import RunsBoard from '$lib/lineage/RunsBoard.svelte';

	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let runs = $state<RunStatus[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);

	const unauthorized = $derived(runs === null && lastStatus === 401);
	const offline = $derived(runs === null && settled && lastStatus !== 401);

	async function load(): Promise<void> {
		const res = await listRuns();
		settled = true;
		if (res.ok) {
			runs = res.data.runs ?? [];
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
	}

	// The board is 330 KB on the live estate (875 runs), and a timer paid that every 5s whether or not
	// a run had moved. A run's state change IS a lineage event, so the cursor is exact here.
	liveRead(lineageTick, () => load());
</script>

<svelte:head><title>Runs · lineage · rask</title></svelte:head>

<RunsBoard {runs} {unauthorized} {offline} {loginHref} />
