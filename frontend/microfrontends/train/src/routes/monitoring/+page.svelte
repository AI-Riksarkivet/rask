<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { Progress } from '@rask/ui/progress';
	import { Activity } from '@lucide/svelte';

	// MONITORING (R17) — honest dummy: the tiles and utilisation rows are the real
	// surface's layout; the numbers are clearly-labeled placeholders until metrics
	// stream from the training service (OTLP → GreptimeDB, same plane as the fleet).
	const tiles = [
		{ id: 'loss', label: 'Train loss', value: '0.142', hint: 'run-0042 · step 18.4k' },
		{ id: 'val-cer', label: 'Validation CER', value: '4.8%', hint: 'best so far: 4.6%' },
		{ id: 'throughput', label: 'Throughput', value: '312 lines/s', hint: 'across 1 × A100' },
		{ id: 'eta', label: 'ETA', value: '~2h 10m', hint: '4 epochs remaining' },
	];

	const gpus = [
		{ id: 'gpu-0', label: 'GPU 0 — A100 80GB', util: 94, mem: 71 },
		{ id: 'gpu-1', label: 'GPU 1 — A100 80GB', util: 0, mem: 2 },
		{ id: 'gpu-2', label: 'GPU 2 — A100 80GB', util: 0, mem: 2 },
	];
</script>

<svelte:head><title>Training monitoring — RASK</title></svelte:head>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-5 p-6">
	<header class="flex items-center gap-3">
		<div
			class="bg-sidebar-primary text-sidebar-primary-foreground flex size-10 items-center justify-center rounded-xl"
		>
			<Activity class="size-5" />
		</div>
		<div class="min-w-0 flex-1">
			<h1 class="text-xl font-semibold tracking-tight">Monitoring</h1>
			<p class="text-muted-foreground text-sm">
				Live metrics for the active run — loss, error rate, hardware.
			</p>
		</div>
		<Badge variant="outline">Placeholder data</Badge>
	</header>

	<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
		{#each tiles as tile (tile.id)}
			<Card class="flex flex-col gap-1 p-4">
				<span class="text-muted-foreground text-xs">{tile.label}</span>
				<span class="text-2xl font-semibold tracking-tight">{tile.value}</span>
				<span class="text-muted-foreground text-[11px]">{tile.hint}</span>
			</Card>
		{/each}
	</div>

	<Card class="flex flex-col gap-4 p-5">
		<h2 class="text-sm font-medium">GPU utilisation (placeholder)</h2>
		{#each gpus as gpu (gpu.id)}
			<div class="flex flex-col gap-1.5">
				<div class="text-muted-foreground flex items-center justify-between text-xs">
					<span>{gpu.label}</span>
					<span>{gpu.util}% util · {gpu.mem}% mem</span>
				</div>
				<Progress value={gpu.util} />
			</div>
		{/each}
	</Card>

	<p class="text-muted-foreground text-xs">
		Placeholder numbers — the live feed arrives with the training service's metrics export.
	</p>
</div>
