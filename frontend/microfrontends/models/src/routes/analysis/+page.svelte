<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { Table } from '@rask/ui/table';
	import { ChartLine, Trophy } from '@lucide/svelte';

	// ANALYSIS (R17) — honest dummy: compare finished runs checkpoint by checkpoint.
	// The comparison table and the best-checkpoint call-out are the real layout; the
	// metrics are clearly-labeled placeholders until evaluation results land.
	const checkpoints = [
		{ id: 'run-0040/ep10', run: 'trocr-court-records-v1', epoch: 10, cer: '4.6%', wer: '13.2%' },
		{ id: 'run-0040/ep08', run: 'trocr-court-records-v1', epoch: 8, cer: '4.9%', wer: '13.9%' },
		{ id: 'run-0038/ep12', run: 'trocr-baseline', epoch: 12, cer: '6.1%', wer: '16.4%' },
		{ id: 'run-0039/ep03', run: 'htrflow-e2e-pilot', epoch: 3, cer: '9.7%', wer: '24.1%' },
	];
	const best = checkpoints[0];
</script>

<svelte:head><title>Training analysis — rask</title></svelte:head>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-5 p-6">
	<header class="flex items-center gap-3">
		<div
			class="bg-sidebar-primary text-sidebar-primary-foreground flex size-10 items-center justify-center rounded-xl"
		>
			<ChartLine class="size-5" />
		</div>
		<div class="min-w-0 flex-1">
			<h1 class="text-xl font-semibold tracking-tight">Analysis</h1>
			<p class="text-muted-foreground text-sm">
				Compare finished runs — error rates per checkpoint, best candidate first.
			</p>
		</div>
		<Badge variant="outline">Placeholder data</Badge>
	</header>

	{#if best}
		<Card class="flex items-center gap-3 p-4">
			<div class="bg-muted text-foreground flex size-10 items-center justify-center rounded-lg">
				<Trophy class="size-5" />
			</div>
			<div class="min-w-0 flex-1">
				<div class="text-sm font-medium">Best checkpoint: {best.id}</div>
				<div class="text-muted-foreground text-xs">
					{best.run} — epoch {best.epoch}, CER {best.cer}, WER {best.wer} (placeholder metrics).
				</div>
			</div>
			<Badge variant="success">candidate</Badge>
		</Card>
	{/if}

	<Card class="overflow-x-auto">
		<Table.Root>
			<Table.Header>
				<Table.Row>
					<Table.Head>Checkpoint</Table.Head>
					<Table.Head>Run</Table.Head>
					<Table.Head>Epoch</Table.Head>
					<Table.Head>CER</Table.Head>
					<Table.Head>WER</Table.Head>
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each checkpoints as cp (cp.id)}
					<Table.Row>
						<Table.Cell class="font-medium">{cp.id}</Table.Cell>
						<Table.Cell class="text-muted-foreground">{cp.run}</Table.Cell>
						<Table.Cell class="text-muted-foreground">{cp.epoch}</Table.Cell>
						<Table.Cell>{cp.cer}</Table.Cell>
						<Table.Cell>{cp.wer}</Table.Cell>
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>
	</Card>

	<p class="text-muted-foreground text-xs">
		Placeholder metrics — real evaluation results flow in from the training service's eval jobs.
	</p>

	<!-- DCGM (owner's ruling 2026-08-05): training analysis gets the GPU lens too — a summary of
	     the same dcgm-exporter view /compute/gpu embeds in full. Honest dummy until wired. -->
	<section class="flex flex-col gap-2 rounded-lg border p-5">
		<div class="flex items-center gap-2">
			<h2 class="font-medium">GPU metrics (DCGM)</h2>
			<span class="text-muted-foreground rounded-full border px-2 py-0.5 text-xs"
				>Dummy — exporter not wired</span
			>
		</div>
		<p class="text-muted-foreground text-sm">
			Per-run GPU utilisation, memory and thermals from the NVIDIA DCGM exporter — the training-run
			slice of the full view at /compute/gpu.
		</p>
		<div
			class="text-muted-foreground/60 grid h-32 place-items-center rounded-lg border border-dashed text-sm"
		>
			dcgm-exporter embed target
		</div>
	</section>
</div>
