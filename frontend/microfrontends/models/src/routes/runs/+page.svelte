<script lang="ts">
	import { Badge, type BadgeVariant } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { Progress } from '@rask/ui/progress';
	import { Table } from '@rask/ui/table';
	import { ListChecks } from '@lucide/svelte';

	// WATCH TRAINING (R17) — honest dummy rows: the table is the real run board's
	// shape (name, base model, state, progress, epoch, started), the data is
	// clearly-labeled placeholder until the training service exists.
	type RunState = 'running' | 'queued' | 'succeeded' | 'failed';
	const STATE_VARIANT: Record<RunState, BadgeVariant> = {
		running: 'default',
		queued: 'secondary',
		succeeded: 'success',
		failed: 'destructive',
	};

	const runs: {
		id: string;
		name: string;
		model: string;
		state: RunState;
		progress: number;
		epoch: string;
		started: string;
	}[] = [
		{
			id: 'run-0042',
			name: 'trocr-court-records-v2',
			model: 'trocr-base-handwritten',
			state: 'running',
			progress: 62,
			epoch: '6 / 10',
			started: '2026-07-27 09:14',
		},
		{
			id: 'run-0041',
			name: 'yolo-lines-church-books',
			model: 'yolov9-lines',
			state: 'queued',
			progress: 0,
			epoch: '— / 25',
			started: '—',
		},
		{
			id: 'run-0040',
			name: 'trocr-court-records-v1',
			model: 'trocr-base-handwritten',
			state: 'succeeded',
			progress: 100,
			epoch: '10 / 10',
			started: '2026-07-26 18:02',
		},
		{
			id: 'run-0039',
			name: 'htrflow-e2e-pilot',
			model: 'htrflow-e2e',
			state: 'failed',
			progress: 31,
			epoch: '3 / 12',
			started: '2026-07-26 11:47',
		},
	];
</script>

<svelte:head><title>Training runs — rask</title></svelte:head>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-5 p-6">
	<header class="flex items-center gap-3">
		<div
			class="bg-sidebar-primary text-sidebar-primary-foreground flex size-10 items-center justify-center rounded-xl"
		>
			<ListChecks class="size-5" />
		</div>
		<div class="min-w-0 flex-1">
			<h1 class="text-xl font-semibold tracking-tight">Training runs</h1>
			<p class="text-muted-foreground text-sm">Every submitted run, live state first.</p>
		</div>
		<Badge variant="outline">Placeholder data</Badge>
	</header>

	<Card class="overflow-x-auto">
		<Table.Root>
			<Table.Header>
				<Table.Row>
					<Table.Head>Run</Table.Head>
					<Table.Head>Base model</Table.Head>
					<Table.Head>State</Table.Head>
					<Table.Head class="w-40">Progress</Table.Head>
					<Table.Head>Epoch</Table.Head>
					<Table.Head>Started</Table.Head>
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each runs as run (run.id)}
					<Table.Row>
						<Table.Cell class="font-medium">{run.name}</Table.Cell>
						<Table.Cell class="text-muted-foreground">{run.model}</Table.Cell>
						<Table.Cell>
							<Badge variant={STATE_VARIANT[run.state]}>{run.state}</Badge>
						</Table.Cell>
						<Table.Cell><Progress value={run.progress} /></Table.Cell>
						<Table.Cell class="text-muted-foreground">{run.epoch}</Table.Cell>
						<Table.Cell class="text-muted-foreground">{run.started}</Table.Cell>
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>
	</Card>

	<p class="text-muted-foreground text-xs">
		Placeholder rows — the live board reads the training service's run feed once it lands.
	</p>
</div>
