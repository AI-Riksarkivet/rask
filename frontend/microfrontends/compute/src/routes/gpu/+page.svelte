<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { CircuitBoard } from '@lucide/svelte';

	// GPU METRICS (DCGM) — the owner's ruling (2026-08-05): the dcgm-exporter view embeds HERE
	// (and a sibling summary in the models zone's Analysis). Wired as an HONEST dummy until the
	// exporter + a Perses/Grafana panel URL exist to embed — a placeholder that names its data
	// source beats an empty iframe pretending.
</script>

<svelte:head><title>GPU · compute · rask</title></svelte:head>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-5 p-6">
	<header class="flex items-center gap-3">
		<div class="bg-sidebar-primary text-sidebar-primary-foreground flex size-10 items-center justify-center rounded-xl">
			<CircuitBoard class="size-5" />
		</div>
		<div class="min-w-0 flex-1">
			<h1 class="text-xl font-semibold tracking-tight">GPU metrics</h1>
			<p class="text-muted-foreground text-sm">Per-device utilisation, memory and thermals — NVIDIA DCGM.</p>
		</div>
		<Badge variant="outline">Dummy — exporter not wired</Badge>
	</header>

	<Card class="flex flex-col gap-2 p-5">
		<h2 class="font-medium">DCGM exporter view</h2>
		<p class="text-muted-foreground text-sm">
			This panel will embed the dcgm-exporter dashboard (per-GPU utilisation, SM occupancy, memory,
			power, thermals) for the Ray cluster's nodes. Waiting on: the dcgm-exporter DaemonSet in the
			chart and a panel URL from the observability stack.
		</p>
		<div class="text-muted-foreground/60 grid h-48 place-items-center rounded-lg border border-dashed text-sm">
			dcgm-exporter embed target
		</div>
	</Card>
</div>
