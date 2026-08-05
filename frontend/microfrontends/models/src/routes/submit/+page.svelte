<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { Field } from '@rask/ui/field';
	import { Input } from '@rask/ui/input';
	import { Select, type SelectOption } from '@rask/ui/select';
	import { Separator } from '@rask/ui/separator';
	import { Rocket } from '@lucide/svelte';

	// SUBMIT TRAINING — a form scaffold (R17). Honest dummy: the fields are the real
	// submission surface's shape, but nothing is wired to a backend yet — the submit
	// button says so instead of pretending.
	// The training-stack CANDIDATES (owner's shortlist, 2026-08-05): the submission surface will
	// drive one — or several — of these. Linked at the top of the page so evaluating them is one
	// click from where training will actually be submitted.
	const TRAINING_STACKS = [
		{
			name: 'transformers',
			href: 'https://github.com/huggingface/transformers',
			blurb: 'The HF baseline — Trainer + TrOCR/YOLO ecosystems we already ship.',
		},
		{
			name: 'Unsloth',
			href: 'https://github.com/unslothai/unsloth',
			blurb: 'Fast single-GPU LoRA/QLoRA fine-tuning.',
		},
		{
			name: 'ms-swift',
			href: 'https://github.com/modelscope/ms-swift',
			blurb: "ModelScope's multi-modal fine-tuning stack (500+ LLM/MLLM recipes).",
		},
		{
			name: 'Axolotl',
			href: 'https://github.com/axolotl-ai-cloud/axolotl',
			blurb: 'YAML-config post-training: full FT, LoRA, DPO/RL.',
		},
	];

	const BASE_MODELS: SelectOption[] = [
		{ value: 'trocr-base-handwritten', label: 'TrOCR base (handwritten)' },
		{ value: 'yolov9-lines', label: 'YOLOv9 line detector' },
		{ value: 'htrflow-e2e', label: 'HTRflow end-to-end' },
	];
	const DATASETS: SelectOption[] = [
		{ value: 'gold-court-records-1600s', label: 'gold/court-records-1600s (placeholder)' },
		{ value: 'gold-church-books', label: 'gold/church-books (placeholder)' },
		{ value: 'silver-police-reports', label: 'silver/police-reports (placeholder)' },
	];
	const GPU_SHAPES: SelectOption[] = [
		{ value: '1xA100', label: '1 × A100' },
		{ value: '3xA100', label: '3 × A100 (full node)' },
	];

	let runName = $state('');
	let baseModel = $state('trocr-base-handwritten');
	let dataset = $state('');
	let epochs = $state('10');
	let learningRate = $state('5e-5');
	let gpuShape = $state('1xA100');

	const baseModelLabel = $derived(
		BASE_MODELS.find((m) => m.value === baseModel)?.label ?? baseModel,
	);
	const datasetLabel = $derived(DATASETS.find((d) => d.value === dataset)?.label ?? '—');
</script>

<svelte:head><title>Submit training — RASK</title></svelte:head>

<div class="mx-auto flex w-full max-w-3xl flex-col gap-5 p-6">
	<header class="flex items-center gap-3">
		<div
			class="bg-sidebar-primary text-sidebar-primary-foreground flex size-10 items-center justify-center rounded-xl"
		>
			<Rocket class="size-5" />
		</div>
		<div class="min-w-0 flex-1">
			<h1 class="text-xl font-semibold tracking-tight">Submit training</h1>
			<p class="text-muted-foreground text-sm">
				Fine-tune a layout or transcription model on a governed dataset.
			</p>
		</div>
		<Badge variant="outline">Scaffold — not wired</Badge>
	</header>

	<!-- Owner's ask (2026-08-05): the four candidate stacks, at the TOP of submit. -->
	<Card class="flex flex-col gap-3 p-5">
		<div class="flex items-center gap-2">
			<h2 class="font-medium">Training stacks under evaluation</h2>
			<Badge variant="outline">we adopt one — or all four</Badge>
		</div>
		<div class="grid gap-2 sm:grid-cols-2">
			{#each TRAINING_STACKS as stack (stack.name)}
				<a
					href={stack.href}
					target="_blank"
					rel="noopener noreferrer"
					class="hover:bg-accent/40 focus-visible:ring-ring flex flex-col gap-0.5 rounded-lg border p-3 transition-colors focus-visible:outline-none focus-visible:ring-2"
				>
					<span class="font-medium">{stack.name} ↗</span>
					<span class="text-muted-foreground text-xs">{stack.blurb}</span>
				</a>
			{/each}
		</div>
	</Card>

	<Card class="flex flex-col gap-4 p-5">
		<Field label="Run name" description="A human-readable handle for the run.">
			<Input placeholder="e.g. trocr-court-records-v2" bind:value={runName} />
		</Field>

		<div class="grid gap-4 sm:grid-cols-2">
			<Field label="Base model">
				<Select bind:value={baseModel} options={BASE_MODELS} ariaLabel="Base model" />
			</Field>
			<Field label="Dataset" description="Placeholder rows — the real list comes from the catalog.">
				<Select
					bind:value={dataset}
					options={DATASETS}
					placeholder="Pick a dataset…"
					ariaLabel="Dataset"
				/>
			</Field>
		</div>

		<div class="grid gap-4 sm:grid-cols-3">
			<Field label="Epochs">
				<Input type="number" min="1" bind:value={epochs} />
			</Field>
			<Field label="Learning rate">
				<Input bind:value={learningRate} />
			</Field>
			<Field label="GPUs">
				<Select bind:value={gpuShape} options={GPU_SHAPES} ariaLabel="GPUs" />
			</Field>
		</div>

		<Separator />

		<div class="flex items-center justify-between gap-3">
			<p class="text-muted-foreground text-xs">
				Would submit <span class="text-foreground font-medium">{runName || 'unnamed run'}</span>
				— {baseModelLabel} on {datasetLabel}, {epochs} epochs at {learningRate} on {gpuShape}.
			</p>
			<Button disabled title="Submission wiring lands with the training service">
				<Rocket class="size-4" /> Submit run
			</Button>
		</div>
	</Card>

	<p class="text-muted-foreground text-xs">
		Placeholder surface: the form is the intended submission contract; the training service that
		accepts it is a follow-up.
	</p>
</div>
