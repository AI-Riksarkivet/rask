<script lang="ts">
	// The designer's LIVE PREVIEW — the workspace the current draft would build, updating as
	// either view (form or YAML) edits the same draft. Each strip mirrors a real surface the
	// ontology drives: the drawing rail, the quick-label chips, the classification bar, the
	// span keymap, the inspector's transcription/typed fields, the link rail. It previews the
	// CONTROL surfaces, not the canvas — pixels come from the item, and the contract is what a
	// template decides.
	import { Badge } from '@rask/ui/badge';
	import { Link2, Square, SquareDashed, Timer, Waypoints } from '@lucide/svelte';
	import { previewModel } from './task-preview.js';
	import type { TaskDraft } from './task-yaml.js';

	let { draft }: { draft: TaskDraft } = $props();
	const model = $derived(previewModel(draft));
	const TOOL_ICONS: Record<string, typeof Square> = {
		bbox: Square,
		polygon: Waypoints,
		mask: SquareDashed,
		segment: Timer,
	};
</script>

<div
	class="border-border flex flex-col gap-2 rounded-md border border-dashed p-2 text-xs"
	data-testid="task-preview"
>
	<span class="text-muted-foreground text-[10px] font-medium tracking-wide uppercase">
		Preview — what annotators get
	</span>
	{#if model.empty}
		<p class="text-muted-foreground" data-testid="preview-empty">
			No labels yet — the workspace would be unconstrained (every tool, free-text labels).
		</p>
	{:else}
		{#if model.draw.length}
			<div class="flex flex-wrap items-center gap-1" data-testid="preview-toolbar">
				<span class="text-muted-foreground w-16 shrink-0 text-[10px]">toolbar</span>
				{#each model.draw as tool (tool)}
					{@const Icon = TOOL_ICONS[tool] ?? Square}
					<span
						class="border-border bg-background inline-flex items-center gap-1 rounded border px-1.5 py-0.5"
					>
						<Icon class="size-3" />
						{tool}
					</span>
				{/each}
			</div>
		{/if}
		{#if model.labels.length}
			<div class="flex flex-wrap items-center gap-1" data-testid="preview-labels">
				<span class="text-muted-foreground w-16 shrink-0 text-[10px]">labels</span>
				{#each model.labels as label (label.name)}
					<Badge variant="outline">
						{label.name}{#if label.required}<span
								class="text-destructive"
								title="required on every completed item">*</span
							>{/if}
					</Badge>
				{/each}
			</div>
		{/if}
		{#if model.tags.length}
			<div class="flex flex-wrap items-center gap-1" data-testid="preview-tags">
				<span class="text-muted-foreground w-16 shrink-0 text-[10px]">item tags</span>
				{#each model.tags as tag (tag)}
					<Badge variant="secondary">{tag}</Badge>
				{/each}
			</div>
		{/if}
		{#if model.spans.length}
			<div class="flex flex-wrap items-center gap-1" data-testid="preview-spans">
				<span class="text-muted-foreground w-16 shrink-0 text-[10px]">text spans</span>
				{#each model.spans.slice(0, 9) as span, i (span)}
					<span
						class="border-border bg-background inline-flex items-center gap-1 rounded border px-1.5 py-0.5"
					>
						<kbd class="text-muted-foreground font-mono text-[9px]">{i + 1}</kbd>
						{span}
					</span>
				{/each}
			</div>
		{/if}
		{#each model.inspectors as row (row.class)}
			<div class="flex flex-wrap items-center gap-1" data-testid="preview-inspector">
				<span class="text-muted-foreground w-16 shrink-0 text-[10px]">inspector</span>
				<span class="font-medium">{row.class}:</span>
				{#if row.transcribe}
					<Badge variant="outline">transcription</Badge>
				{/if}
				{#each row.fields as field (field.name)}
					<span class="border-border bg-background rounded border px-1.5 py-0.5">
						{field.name}
						<span class="text-muted-foreground">
							{field.options.length ? field.options.join(' | ') : field.type}{field.required ? ' *' : ''}
						</span>
					</span>
				{/each}
			</div>
		{/each}
		{#if model.relations.length}
			<div class="flex flex-wrap items-center gap-1" data-testid="preview-relations">
				<span class="text-muted-foreground w-16 shrink-0 text-[10px]">relations</span>
				{#each model.relations as rel (rel.name)}
					<span
						class="border-border bg-background inline-flex items-center gap-1 rounded border px-1.5 py-0.5"
					>
						<Link2 class="size-3" />
						{rel.name}
						<span class="text-muted-foreground">{rel.from.join('/')} → {rel.to.join('/')}</span>
					</span>
				{/each}
			</div>
		{/if}
	{/if}
</div>
