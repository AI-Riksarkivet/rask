<script lang="ts">
	// The maintenance-policy form's fields: the four bounds, the enabled switch, and Save/Cancel. The
	// namespace page and the project page edit the same draft shape against their own doors, so the
	// fields are written once; the caller owns the draft, the write and what a refusal says.
	import type { Snippet } from 'svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { Checkbox } from '@riksarkivet/ui/checkbox';
	import { Input } from '@riksarkivet/ui/input';
	import type { PolicyDraft } from '#lib/policies.ts';

	let {
		draft = $bindable(),
		busy,
		onsave,
		oncancel,
		children,
	}: {
		/** number | null is what bind:value on a type="number" input delivers (Svelte 5 coerces, or
		 *  null for an empty field), and null is what "inherit" means to `policyRequestFrom`. */
		draft: PolicyDraft;
		busy: boolean;
		onsave: () => void;
		oncancel: () => void;
		/** A note under the fields, at the form's full width. */
		children?: Snippet;
	} = $props();

	/** A number field: a fixed width, dense. */
	const NUMBER = 'h-7 w-32 font-mono text-xs';
	const LABEL = 'flex flex-col gap-1';
</script>

<div class="text-muted-foreground flex flex-wrap items-end gap-3 text-xs">
	<label class={LABEL}
		>retention days <Input
			class={NUMBER}
			type="number"
			min="1"
			bind:value={draft.retention_days}
			placeholder="global default"
		/></label
	>
	<label class={LABEL}
		>retain versions <Input
			class={NUMBER}
			type="number"
			min="1"
			bind:value={draft.retain_versions}
			placeholder="—"
		/></label
	>
	<label class={LABEL}
		>compact every (h) <Input
			class={NUMBER}
			type="number"
			min="1"
			bind:value={draft.interval}
			placeholder="every sweep"
		/></label
	>
	<label class={LABEL}
		>target rows/fragment <Input
			class={NUMBER}
			type="number"
			min="1024"
			bind:value={draft.target}
			placeholder="Lance default"
		/></label
	>
	<label class="flex items-center gap-1.5"
		><Checkbox bind:checked={draft.enabled} /> maintenance enabled</label
	>
	<div class="flex gap-2">
		<Button size="sm" disabled={busy} onclick={onsave}>Save policy</Button>
		<Button variant="ghost" size="sm" onclick={oncancel}>Cancel</Button>
	</div>
	{#if children}<div class="max-w-[62ch] basis-full">{@render children()}</div>{/if}
</div>
