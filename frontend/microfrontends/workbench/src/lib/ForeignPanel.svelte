<script lang="ts">
	/**
	 * The compositor's HOST for one zone-owned custom element (open_workbench.md). This file is the
	 * whole cross-zone mechanism: dynamic-import the owning zone's element script by URL, wait for
	 * the tag's definition, then render the tag. The element arrives styled by the page's token
	 * cascade (light DOM) and talks back only via the RASK_SELECT CustomEvent — this wrapper owns
	 * every dock-facing concern (loading state, failure surface), so dockview itself never learns
	 * that the panel is foreign.
	 *
	 * `params` comes from `addPanel({ params })` and is MUTATED IN PLACE by the renderer — read
	 * fields lazily, never destructure at init.
	 */
	import type { PanelProps } from '@rask/dockview';

	let { params }: PanelProps<{ src: string; tag: string }> = $props();

	let phase = $state<'loading' | 'ready' | 'failed'>('loading');
	let detail = $state('');

	$effect(() => {
		const { src, tag } = params;
		if (!src || !tag) return;
		let live = true;
		(async () => {
			try {
				await import(/* @vite-ignore */ src);
				await customElements.whenDefined(tag);
				if (live) phase = 'ready';
			} catch (e) {
				if (!live) return;
				phase = 'failed';
				detail = e instanceof Error ? e.message : String(e);
			}
		})();
		return () => {
			live = false;
		};
	});
</script>

{#if phase === 'ready'}
	<svelte:element this={params.tag} class="foreign" />
{:else if phase === 'failed'}
	<div class="state">
		<p>Could not load this panel from its zone.</p>
		<p class="dim">{params.src} — {detail}</p>
	</div>
{:else}
	<div class="state dim">Loading {params.tag}…</div>
{/if}

<style>
	.foreign {
		display: block;
		height: 100%;
	}
	.state {
		display: grid;
		place-content: center;
		height: 100%;
		text-align: center;
		font-size: 0.8125rem;
		gap: 0.25rem;
	}
	.dim {
		color: var(--color-muted-foreground);
	}
</style>
