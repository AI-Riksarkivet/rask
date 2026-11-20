<script lang="ts">
	/**
	 * The label filter of a catalog list: a select over the label names some object the caller can see
	 * carries. Like the project filter it holds no value of its own (a list's page keeps it in its URL,
	 * `?label=<name>`), and it narrows rows the caller was already given: a view, never a boundary.
	 */
	import { Select } from '@riksarkivet/ui/select';
	import { labelOptions } from './labels.ts';

	let {
		value,
		names,
		onchange,
	}: { value: string; names: readonly string[]; onchange: (label: string) => void } = $props();

	function choose(label: string): void {
		if (label !== value) onchange(label);
	}
</script>

<!-- Named on screen as well as to a screen reader; the visible word is part of the accessible name. -->
<label class="text-muted-foreground inline-flex items-center gap-2 text-sm">
	Label
	<Select
		{value}
		ariaLabel="Label filter"
		placeholder="any label"
		options={labelOptions(names, value)}
		onValueChange={choose}
	/>
</label>
