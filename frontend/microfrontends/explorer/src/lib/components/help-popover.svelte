<script lang="ts">
	// The search-bar `?` — a small popover with click-to-run examples + tips that
	// make search easier.
	//
	// It used to end with a link to a /guide page: 2 091 lines of prose about how search works.
	// That page was already gone from the SIDEBAR (nav.ts: "documentation is not an area of the
	// corpus"), leaving this popover as its only entrance, and the whole route was deleted with it.
	// What survives is what earned its place — examples you can CLICK TO RUN, next to the box you
	// would type them into.
	import { Popover } from 'bits-ui';
	import { HelpCircle, Image as ImageIcon } from '@lucide/svelte';
	import { buttonVariants } from '@rask/ui';

	type Example = { label: string; example: string; explain: string };
	type Props = {
		examples: Record<string, Example>;
		/** Called when the user clicks an example row — fires (key, example). */
		onpick?: (key: string, example: string) => void;
	};
	let { examples, onpick }: Props = $props();

	let open = $state(false);

	function pick(key: string, example: string) {
		onpick?.(key, example);
		open = false; // filled the query box — close so results are visible
	}
</script>

<Popover.Root bind:open>
	<Popover.Trigger
		class={buttonVariants({ variant: 'ghost', size: 'icon-sm' })}
		title="Examples & tips"
		aria-label="Examples and tips"
	>
		<HelpCircle />
	</Popover.Trigger>
	<Popover.Portal>
		<Popover.Content
			side="bottom"
			align="end"
			sideOffset={6}
			class="border-border bg-popover text-popover-foreground z-50 w-[min(92vw,400px)] rounded-lg border p-3 text-xs shadow-md"
		>
			<div class="text-foreground mb-1.5 font-medium">Try an example</div>
			<div class="grid gap-0.5">
				{#each Object.entries(examples) as [key, info] (key)}
					<button
						type="button"
						onclick={() => pick(key, info.example)}
						class="hover:bg-muted grid grid-cols-[84px_1fr] items-baseline gap-2 rounded-md px-2 py-1.5 text-left transition-colors"
					>
						<span class="text-foreground font-medium">{info.label}</span>
						<span class="min-w-0">
							<code class="bg-muted text-primary rounded-md px-1.5 py-0.5 font-mono text-xs">
								{info.example}
							</code>
							<span class="text-muted-foreground mt-0.5 block">{info.explain}</span>
						</span>
					</button>
				{/each}
			</div>

			<div
				class="border-border bg-muted/40 text-muted-foreground mt-2 flex items-start gap-1.5 rounded-lg border border-dashed p-2"
			>
				<ImageIcon class="mt-0.5 size-3.5 shrink-0" />
				<span
					><strong class="text-foreground">Attach an image</strong> (📎 or drag in) to add a visual pass.</span
				>
			</div>
		</Popover.Content>
	</Popover.Portal>
</Popover.Root>
