<script lang="ts">
	// Layers section (lives inside the sidebar). Group-by column + per-group
	// visibility + color, bound to the facade's LayerStore. (Ported from ra-anno.)
	import { Eye, EyeOff, ChevronDown, ChevronRight, Layers } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { Select } from '@rask/ui/select';
	import { cn } from '@rask/ui/utils';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	let collapsed = $state(false);

	// The estate Select is bind-only (bits-ui), so the trigger's value is a WRITABLE $derived of
	// the controller's column. It tracks the controller for free (loading a unit re-seeds
	// groupByColumn from the layer store, and the trigger follows instead of going stale), while
	// a user pick writes over it locally until the effect pushes that pick down. The guard keeps
	// setGroupBy from re-entering on the controller's own echo.
	let groupChoice = $derived(controller.groupByColumn);
	$effect(() => {
		if (groupChoice && groupChoice !== controller.groupByColumn) controller.setGroupBy(groupChoice);
	});
</script>

<section class="border-border border-t" data-testid="layer-panel">
	<button
		class="text-muted-foreground hover:text-foreground flex w-full items-center gap-1.5 px-3 py-2 text-xs font-medium transition-colors"
		onclick={() => (collapsed = !collapsed)}
	>
		{#if collapsed}<ChevronRight class="size-3.5" />{:else}<ChevronDown class="size-3.5" />{/if}
		<Layers class="size-3.5" />
		Layers
	</button>

	{#if !collapsed}
		<div class="flex flex-col gap-2 px-3 pb-3">
			<div class="flex items-center gap-2">
				<span class="text-muted-foreground shrink-0 text-xs">Group by</span>
				<div class="min-w-0 flex-1">
					<Select
						options={controller.groupColumns.map((c) => ({ value: c, label: c }))}
						bind:value={groupChoice}
						ariaLabel="Group annotations by column"
					/>
				</div>
			</div>

			<ul class="flex flex-col gap-0.5">
				{#each controller.groups as g (g.name)}
					<li class="hover:bg-muted/50 flex items-center gap-2 rounded-md px-1 py-0.5">
						<Button
							variant="ghost"
							size="icon-xs"
							title={controller.isHidden(g.name) ? 'Show' : 'Hide'}
							onclick={() => controller.toggleGroupVisible(g.name)}
						>
							{#if controller.isHidden(g.name)}<EyeOff class="size-3.5" />{:else}<Eye
									class="size-3.5"
								/>{/if}
						</Button>
						<input
							type="color"
							class="size-4 shrink-0 cursor-pointer rounded border-0 bg-transparent p-0"
							value={controller.groupColorHex(g.name)}
							title="Group color"
							onchange={(e) => controller.setGroupColor(g.name, e.currentTarget.value)}
						/>
						<span
							class={cn(
								'flex-1 truncate text-xs',
								controller.isHidden(g.name) && 'text-muted-foreground line-through',
							)}
							title={g.name}
						>
							{g.name || '∅'}
						</span>
						<span class="text-muted-foreground text-xs tabular-nums">{g.count}</span>
					</li>
				{/each}
			</ul>
		</div>
	{/if}
</section>
