<script lang="ts">
	// One rendered principal: the FULL id, and a control that copies it. The console has no directory
	// to resolve an id against (the issuer's users are its own, and no lakehouse door names them), so
	// the id is what a person reads, compares and pastes into a grant; shortening it would hide the
	// one value that is useful. The copy writes through the asynchronous Clipboard API, which the
	// console's CSP does not restrict, on the click that asks for it.
	import type { ClassValue } from 'svelte/elements';
	import { CopyButton } from '@rask/ui/copy-button';

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
</script>

<span data-slot="subject" class={['subject', className]}>
	<span class="id">{shown}</span>
	{#if value}
		<CopyButton {value} label={value} class="nodrag" />
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
