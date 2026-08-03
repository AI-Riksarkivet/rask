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
	import { resolveForeign } from './catalogue';

	let { params, api }: PanelProps<{ src?: string; tag?: string }> = $props();

	/** Resolved by the panel's COMPONENT key (stable through picker-adds, duplicates and layout
	 *  restores), with the live catalogue outranking persisted params — both review findings. */
	const source = $derived(resolveForeign(api.component, params));

	let phase = $state<'loading' | 'ready' | 'failed'>('loading');
	let detail = $state('');
	/** Bumped by the Retry button — read inside the effect, so a failed import (a rolling zone
	 *  deploy's 15-second window, observed live) re-runs without remounting the whole panel. */
	let attempt = $state(0);

	$effect(() => {
		void attempt;
		if (source === null) {
			phase = 'failed';
			detail = `no catalogue entry for "${api.component}"`;
			return;
		}
		const { src, tag } = source;
		let live = true;
		(async () => {
			try {
				await import(/* @vite-ignore */ src);
				// Bounded: a script that loads but never defines the tag (zone/catalogue drift) must
				// surface as failure, not spin forever — review finding.
				await Promise.race([
					customElements.whenDefined(tag),
					new Promise((_, reject) =>
						setTimeout(
							() => reject(new Error(`"${tag}" never registered — zone/catalogue drift?`)),
							8000,
						),
					),
				]);
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

{#if phase === 'ready' && source !== null}
	<svelte:element this={source.tag} class="foreign" />
{:else if phase === 'failed'}
	<div class="state">
		<p>Could not load this panel from its zone.</p>
		<p class="dim">{source?.src ?? api.id} — {detail}</p>
	</div>
{:else}
	<div class="state dim">Loading {source?.tag ?? api.id}…</div>
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
	.retry {
		justify-self: center;
		margin-top: 0.25rem;
		padding: 0.25rem 0.75rem;
		border: 1px solid var(--color-border);
		border-radius: 0.375rem;
		background: transparent;
		color: var(--color-foreground);
		font-size: 0.8125rem;
		cursor: pointer;
	}
	.retry:hover {
		background: var(--color-muted);
	}
</style>
