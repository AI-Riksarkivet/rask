<script lang="ts">
	// The search-bar `?` — a small popover with click-to-run examples + tips that
	// make search easier. The deep "how it works" walk-through now lives on the
	// Guide page (linked at the bottom), so this stays compact.
	import { Popover } from 'bits-ui';
	import { HelpCircle, ArrowRight, Image as ImageIcon } from '@lucide/svelte';
	import { buttonVariants } from '@rask/ui';
	import { base } from '$app/paths';

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

			<a
				href="{base}/guide"
				onclick={() => (open = false)}
				class="text-primary hover:bg-muted mt-2 flex items-center gap-1.5 rounded-md px-2 py-1.5 font-medium transition-colors"
			>
				Full guide: how search works
				<ArrowRight class="size-3.5" />
			</a>
		</Popover.Content>
	</Popover.Portal>
</Popover.Root>
