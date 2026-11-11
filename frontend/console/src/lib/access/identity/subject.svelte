<script lang="ts">
	// One rendered principal: the FULL id, and a control that copies it. The console has no directory
	// to resolve an id against (the issuer's users are its own, and no lakehouse door names them), so
	// the id is what a person reads, compares and pastes into a grant; shortening it would hide the
	// one value that is useful. The copy writes through the asynchronous Clipboard API, which the
	// console's CSP does not restrict, on the click that asks for it.
	import { Check, Copy } from '@lucide/svelte';
	import type { ClassValue } from 'svelte/elements';
	import { Button } from '@rask/ui/button';

	let {
		value,
		fallback = 'system',
		class: className = undefined,
	}: {
		/** The raw subject — an FGA subject, an OIDC sub, or `null` for a system/unattributed action. */
		value: string | null | undefined;
		/** What to render when there is no subject at all. `system` reads correctly for control events. */
		fallback?: string;
		/** Classes for the root, in clsx form. A parent's scoped class never reaches a child's element,
		 *  so a caller styles a subject with utilities. */
		class?: ClassValue;
	} = $props();

	const shown = $derived(value ?? fallback);
	let outcome = $state<'idle' | 'copied' | 'failed'>('idle');

	async function copy(): Promise<void> {
		try {
			await navigator.clipboard.writeText(shown);
			outcome = 'copied';
		} catch {
			outcome = 'failed';
		}
	}
</script>

<span data-slot="subject" class={['subject', className]}>
	<span class="id">{shown}</span>
	{#if value}
		<Button
			variant="ghost"
			size="icon-xs"
			class="nodrag shrink-0"
			aria-label={outcome === 'copied' ? `Copied ${shown}` : `Copy ${shown}`}
			title={outcome === 'failed'
				? 'The browser refused the copy: select the id instead'
				: 'Copy id'}
			onclick={(e: MouseEvent) => {
				// A principal can sit inside a clickable row: copying it opens nothing else.
				e.stopPropagation();
				void copy();
			}}
			onmouseleave={() => (outcome = 'idle')}
		>
			{#if outcome === 'copied'}<Check aria-hidden="true" />{:else}<Copy aria-hidden="true" />{/if}
		</Button>
	{/if}
</span>

<style>
	.subject {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		min-width: 0;
		max-width: 100%;
		vertical-align: middle;
	}
	.id {
		font-family: ui-monospace, monospace;
		overflow-wrap: anywhere;
		user-select: all;
	}
</style>
