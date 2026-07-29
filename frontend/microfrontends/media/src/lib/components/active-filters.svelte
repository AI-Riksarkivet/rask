<script lang="ts">
	import { activeView, type SearchSpec } from '@rask/media-api';
	import { Badge, Button } from '@rask/ui';
	import { X } from '@lucide/svelte';

	type Props = {
		spec: SearchSpec;
		onchange?: (spec: SearchSpec) => void;
	};
	let { spec = $bindable(), onchange }: Props = $props();

	/** Visible filter pills with X buttons. Used to fix the "filter stuck
      invisibly behind a popover" bug. Structured filter fields come from the
      descriptor (`filterFields`); their labels reuse the declared metadata
      labels, falling back to the field name. */
	type Pill =
		| { id: string; kind: 'filter'; field: string; label: string; value: string }
		| { id: string; kind: 'topic' | 'where'; label: string; value: string };
	const pills = $derived.by<Pill[]>(() => {
		const view = activeView();
		const labelFor = (field: string): string =>
			view.metadataFields.find((m) => m.field === field)?.label ?? field;
		const out: Pill[] = [];
		const filters = spec.filters ?? {};
		for (const field of view.filterFields) {
			const value = filters[field];
			if (value)
				out.push({ id: `filter:${field}`, kind: 'filter', field, label: labelFor(field), value });
		}
		if (spec.topic) out.push({ id: 'topic', kind: 'topic', label: 'Topic', value: spec.topic });
		if (spec.where) {
			const expr = spec.where.length > 48 ? `${spec.where.slice(0, 48)}…` : spec.where;
			out.push({ id: 'where', kind: 'where', label: 'SQL', value: expr });
		}
		return out;
	});

	function clear(pill: Pill) {
		if (pill.kind === 'topic') spec = { ...spec, topic: undefined };
		else if (pill.kind === 'where') spec = { ...spec, where: undefined };
		else if (pill.kind === 'filter') {
			const rest = { ...spec.filters };
			delete rest[pill.field];
			spec = { ...spec, filters: Object.keys(rest).length > 0 ? rest : undefined };
		}
		onchange?.(spec);
	}

	function clearAll() {
		spec = { ...spec, filters: undefined, topic: undefined, where: undefined };
		onchange?.(spec);
	}
</script>

{#if pills.length}
	<div class="flex flex-wrap items-center gap-1.5 px-6 pb-3 text-xs">
		<span class="text-muted-foreground">Active filters:</span>
		{#each pills as p (p.id)}
			<Badge variant="secondary" class="gap-1 py-0.5 pr-0.5 pl-2">
				<span class="text-muted-foreground">{p.label}:</span>
				<span class="max-w-[280px] truncate">{p.value}</span>
				<Button
					variant="ghost"
					size="icon-xs"
					class="hover:text-destructive size-4 rounded-full"
					aria-label="Remove {p.label} filter"
					onclick={() => clear(p)}
				>
					<X />
				</Button>
			</Badge>
		{/each}
		{#if pills.length > 1}
			<Button variant="ghost" size="xs" class="text-muted-foreground" onclick={clearAll}>
				Clear all
			</Button>
		{/if}
	</div>
{/if}
