<script lang="ts">
	// MODEL REGISTRY (R17) — viewing + testing models lives in the TRAIN zone.
	// NOTE (R17): lakehouse's models area (/lakehouse/models — registry, experiments,
	// pipeline) MIGRATES HERE; this page is the scaffold of its destination. The
	// physical migration of that surface (and its data wiring) is a follow-up — until
	// it lands, the link below points at the current registry.
	import { Badge, type BadgeVariant } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { Table } from '@rask/ui/table';
	import { Boxes, ExternalLink, FlaskConical } from '@lucide/svelte';

	type Stage = 'blessed' | 'candidate' | 'archived';
	const STAGE_VARIANT: Record<Stage, BadgeVariant> = {
		blessed: 'success',
		candidate: 'secondary',
		archived: 'outline',
	};

	const models: {
		id: string;
		name: string;
		task: string;
		version: string;
		stage: Stage;
		cer: string;
	}[] = [
		{
			id: 'trocr-court-records',
			name: 'trocr-court-records',
			task: 'transcription',
			version: 'v1 (run-0040/ep10)',
			stage: 'blessed',
			cer: '4.6%',
		},
		{
			id: 'trocr-court-records-v2',
			name: 'trocr-court-records',
			task: 'transcription',
			version: 'v2 (run-0042, training)',
			stage: 'candidate',
			cer: '—',
		},
		{
			id: 'yolov9-lines',
			name: 'yolov9-lines',
			task: 'line detection',
			version: 'v3',
			stage: 'blessed',
			cer: 'n/a',
		},
		{
			id: 'trocr-baseline',
			name: 'trocr-baseline',
			task: 'transcription',
			version: 'v1',
			stage: 'archived',
			cer: '6.1%',
		},
	];
</script>

<svelte:head><title>Model registry — RASK</title></svelte:head>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-5 p-6">
	<header class="flex items-center gap-3">
		<div
			class="bg-sidebar-primary text-sidebar-primary-foreground flex size-10 items-center justify-center rounded-xl"
		>
			<Boxes class="size-5" />
		</div>
		<div class="min-w-0 flex-1">
			<h1 class="text-xl font-semibold tracking-tight">Models</h1>
			<p class="text-muted-foreground text-sm">
				The registry — view a model's versions, test one against a sample.
			</p>
		</div>
		<Badge variant="outline">Placeholder data</Badge>
	</header>

	<Card class="overflow-x-auto">
		<Table.Root>
			<Table.Header>
				<Table.Row>
					<Table.Head>Model</Table.Head>
					<Table.Head>Task</Table.Head>
					<Table.Head>Version</Table.Head>
					<Table.Head>Stage</Table.Head>
					<Table.Head>CER</Table.Head>
					<Table.Head class="text-right">Actions</Table.Head>
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each models as model (model.id)}
					<Table.Row>
						<Table.Cell class="font-medium">{model.name}</Table.Cell>
						<Table.Cell class="text-muted-foreground">{model.task}</Table.Cell>
						<Table.Cell class="text-muted-foreground">{model.version}</Table.Cell>
						<Table.Cell>
							<Badge variant={STAGE_VARIANT[model.stage]}>{model.stage}</Badge>
						</Table.Cell>
						<Table.Cell class="text-muted-foreground">{model.cer}</Table.Cell>
						<Table.Cell class="text-right">
							<Button
								variant="outline"
								size="sm"
								disabled
								title="Model testing wiring lands with the training service"
							>
								<FlaskConical class="size-4" /> Test
							</Button>
						</Table.Cell>
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>
	</Card>

	<p class="text-muted-foreground text-xs">
		Placeholder rows. The governed registry currently lives in the lakehouse zone —
		<a
			href="/lakehouse/models"
			data-sveltekit-reload
			class="text-foreground inline-flex items-center gap-0.5 underline underline-offset-2"
			>open it there <ExternalLink class="size-3" /></a
		>
		— and migrates here per R17 (follow-up).
	</p>
</div>
