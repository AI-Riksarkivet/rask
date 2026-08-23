<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { Field } from '@rask/ui/field';
	import { Input } from '@rask/ui/input';
	import { Separator } from '@rask/ui/separator';
	import { Textarea } from '@rask/ui/textarea';
	import { Rocket } from '@lucide/svelte';
	import { MAX_FEATURES, MODEL_PATTERN, parseFeatureLines } from '$lib/models/train';
	import { submitTraining } from '$lib/models/remote/train.remote';

	// SUBMIT TRAINING — wired to `POST /api/train` on the medallion producer.
	//
	// It was a scaffold that said "not wired" while the door behind it worked, and its fields were an
	// invented vocabulary: dropdowns of ONE modality's models and corpora. The real contract is a
	// model NAME, `stage$name` feature refs and an opaque config — the platform governs any modality,
	// so the form cannot be shaped around one. That is why the selects are gone rather than populated.
	//
	// Validation mirrors the door's own patterns (see `$lib/models/train`), so a malformed request is
	// caught while the person is still looking at the field instead of coming back as a 422.
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

	// Hoisted rather than inlined as mustache literals: both carry characters an attribute cannot
	// hold plainly (a newline, braces), and the linter is right that `{'…'}` in markup is noise.
	const FEATURES_PLACEHOLDER = 'silver$features\ngold$catalog@7';
	const CONFIG_PLACEHOLDER = '{ "epochs": 3 }';

	let model = $state('');
	let featureText = $state('');
	let configText = $state('');

	let busy = $state(false);
	let outcome = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);

	const parsedFeatures = $derived(parseFeatureLines(featureText));
	const modelOk = $derived(MODEL_PATTERN.test(model.trim()));
	const configError = $derived.by(() => {
		const raw = configText.trim();
		if (!raw) return null;
		try {
			const value: unknown = JSON.parse(raw);
			if (value === null || typeof value !== 'object' || Array.isArray(value)) {
				return 'Config must be a JSON object.';
			}
			return null;
		} catch {
			return 'Config is not valid JSON.';
		}
	});
	const canSubmit = $derived(
		!busy &&
			modelOk &&
			parsedFeatures.features.length > 0 &&
			parsedFeatures.features.length <= MAX_FEATURES &&
			parsedFeatures.invalid.length === 0 &&
			configError === null,
	);

	async function submit() {
		if (!canSubmit) return;
		busy = true;
		outcome = null;
		const raw = configText.trim();
		const result = await submitTraining({
			model: model.trim(),
			features: parsedFeatures.features,
			config: raw ? (JSON.parse(raw) as Record<string, unknown>) : {},
		});
		busy = false;
		if (result.ok) {
			// The run is DETACHED — it lands in lineage minutes later, so the honest message points
			// there rather than implying the model already exists.
			outcome = {
				tone: 'ok',
				text: `Submitted. The run is detached — it will appear on the runs board once it starts.`,
			};
			return;
		}
		outcome = {
			tone: 'fail',
			text:
				result.status === 403
					? 'Refused: submitting training needs project admin on the configured project.'
					: result.status === 422
						? `The door refused the request shape. ${result.detail ?? ''}`.trim()
						: `Could not submit (${result.status}). ${result.detail ?? ''}`.trim(),
		};
	}
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
				Train a model on governed feature datasets. The run is dispatched in the background and
				appears on the runs board once it starts.
			</p>
		</div>
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
		<Field
			label="Model"
			description="The name this run trains. Letters, digits, dash and underscore; up to 64 characters."
		>
			<Input placeholder="my-model-v2" bind:value={model} />
			{#if model.trim() && !modelOk}
				<p class="text-destructive text-xs">
					A model name must match {String(MODEL_PATTERN)}.
				</p>
			{/if}
		</Field>

		<Field
			label="Feature datasets"
			description={`One per line, as stage$name — add @version to pin one. Up to ${MAX_FEATURES}.`}
		>
			<Textarea rows={4} placeholder={FEATURES_PLACEHOLDER} bind:value={featureText} />
			{#if parsedFeatures.invalid.length > 0}
				<p class="text-destructive text-xs">
					Not a dataset ref: {parsedFeatures.invalid.join(', ')}
				</p>
			{:else if parsedFeatures.features.length > MAX_FEATURES}
				<p class="text-destructive text-xs">
					{parsedFeatures.features.length} datasets — the door accepts at most {MAX_FEATURES}.
				</p>
			{:else if parsedFeatures.features.length > 0}
				<p class="text-muted-foreground text-xs">
					{parsedFeatures.features.length} dataset{parsedFeatures.features.length === 1 ? '' : 's'}
				</p>
			{/if}
		</Field>

		<Field
			label="Config"
			description="Optional JSON object, passed through to the runner. The platform does not interpret it."
		>
			<Textarea rows={4} placeholder={CONFIG_PLACEHOLDER} bind:value={configText} />
			{#if configError}
				<p class="text-destructive text-xs">{configError}</p>
			{/if}
		</Field>

		<Separator />

		<div class="flex items-center gap-3">
			<Button onclick={submit} disabled={!canSubmit}>
				{busy ? 'Submitting…' : 'Submit training'}
			</Button>
			{#if outcome}
				<p class={outcome.tone === 'ok' ? 'text-sm' : 'text-destructive text-sm'}>{outcome.text}</p>
			{/if}
		</div>
	</Card>
</div>
