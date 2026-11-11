<script lang="ts">
	/**
	 * The project filter of a catalog list: a select over the projects the caller can see. It holds
	 * no value of its own; the page or panel it sits in decides where the value lives (a list's page
	 * keeps it in its URL, `project-filter-url.ts`). A view over rows the caller was already given,
	 * never a boundary (`project-filter.ts`).
	 */
	import { onMount } from 'svelte';
	import { Select } from '@rask/ui/select';
	import { fetchVisibleProjects } from '#lib/remote/warehouses.api.ts';
	import { projectOptions } from './project-filter.ts';

	let { value, onchange }: { value: string; onchange: (project: string) => void } = $props();

	/** The ids the catalog answered; none while the read is out or when it failed, when the select
	 *  still offers every project and the one the filter names. */
	let visible = $state.raw<string[]>([]);

	onMount(() => {
		void fetchVisibleProjects().then((res) => {
			if (res.ok) visible = res.data.map((p) => p.project);
		});
	});

	function choose(project: string): void {
		if (project !== value) onchange(project);
	}
</script>

<Select
	{value}
	ariaLabel="Project filter"
	placeholder="all projects"
	options={projectOptions(visible, value)}
	onValueChange={choose}
/>
