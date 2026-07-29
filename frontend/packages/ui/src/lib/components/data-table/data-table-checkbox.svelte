<script lang="ts">
	import { Checkbox } from '../checkbox/index.js';

	// The selection cell for the `selectionColumn()` helper — a thin bridge from tanstack's
	// selection callbacks to the shared Checkbox (renderComponent can't forward snippets, so this
	// tiny component is the reusable unit).
	let {
		checked = false,
		indeterminate = false,
		onCheckedChange,
		'aria-label': ariaLabel,
	}: {
		checked?: boolean;
		indeterminate?: boolean;
		onCheckedChange?: (checked: boolean) => void;
		'aria-label'?: string;
	} = $props();
</script>

<Checkbox
	{checked}
	{indeterminate}
	aria-label={ariaLabel}
	onCheckedChange={(v) => onCheckedChange?.(v === true)}
	onclick={(e: MouseEvent) => e.stopPropagation()}
/>
