<script lang="ts">
	// A control that copies one value, on the click that asks for it, through the asynchronous Clipboard
	// API (no permission prompt for a user gesture, and nothing a strict CSP restricts). It says what it
	// copied, and when the browser refuses the copy it says to select the text instead, so a refusal is
	// never silent.
	import { Check, Copy } from '@lucide/svelte';
	import type { ClassValue } from 'svelte/elements';
	import { Button } from '../button/index.js';

	let {
		value,
		label,
		class: className = undefined,
	}: {
		/** The text the click puts on the clipboard. */
		value: string;
		/** What the value is, for the control's accessible name: `Copy {label}`. */
		label: string;
		class?: ClassValue;
	} = $props();

	let outcome = $state<'idle' | 'copied' | 'failed'>('idle');

	async function copy(): Promise<void> {
		try {
			await navigator.clipboard.writeText(value);
			outcome = 'copied';
		} catch {
			outcome = 'failed';
		}
	}
</script>

<Button
	data-slot="copy-button"
	variant="ghost"
	size="icon-xs"
	class={['shrink-0', className]}
	aria-label={outcome === 'copied' ? `Copied ${label}` : `Copy ${label}`}
	title={outcome === 'failed'
		? 'The browser refused the copy: select the text instead'
		: `Copy ${label}`}
	onclick={(e: MouseEvent) => {
		// A copy control can sit inside a clickable row: copying opens nothing else.
		e.stopPropagation();
		void copy();
	}}
	onmouseleave={() => (outcome = 'idle')}
>
	{#if outcome === 'copied'}<Check aria-hidden="true" />{:else}<Copy aria-hidden="true" />{/if}
</Button>
