<script lang="ts">
	/**
	 * The project filter of a catalog list: a select over the projects the caller can see. It holds
	 * no value of its own; the page or panel it sits in decides where the value lives (a list's page
	 * keeps it in its URL, `project-filter-url.ts`). A view over rows the caller was already given,
	 * never a boundary (`project-filter.ts`).
	 */
	import { Select } from '@rask/ui/select';
	import { projectOptions } from './project-filter.ts';

	let {
		value,
		visible,
		ofRows,
		onchange,
	}: {
		value: string;
		/** The projects the caller can see; none while that read is out or after it failed. */
		visible: readonly string[];
		/** The projects of the rows the list was answered, which the caller may see without being a
		 *  member of them. */
		ofRows: readonly string[];
		onchange: (project: string) => void;
	} = $props();

	function choose(project: string): void {
		if (project !== value) onchange(project);
	}
</script>

<!-- Named on screen as well as to a screen reader; the visible word is part of the accessible name. -->
<label class="text-muted-foreground inline-flex items-center gap-2 text-sm">
	Project
	<Select
		{value}
		ariaLabel="Project filter"
		placeholder="all projects"
		options={projectOptions(visible, ofRows, value)}
		onValueChange={choose}
	/>
</label>
