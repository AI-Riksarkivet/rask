<script lang="ts">
	// The BULK surface (open-bulk phase 1): one labeling task's items as a grid — media, corpus
	// facts, workflow state and LIVE annotation state per row, annotations fetched only for
	// visible rows. `?task=` names the labeling task (the campaign), same vocabulary as
	// /tasks/[id]; without it the page says where to come from instead of rendering dead chrome.
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import BulkGrid from '$lib/bulk/BulkGrid.svelte';
	import { fetchProject, listTasks } from '$lib/projects/remote/projects.remote';

	const projectId = $derived(page.url.searchParams.get('task'));
	const projectQuery = $derived(projectId ? fetchProject({ projectId }) : null);
	const tasksQuery = $derived(projectId ? listTasks({ projectId, details: true }) : null);
	const project = $derived.by(() => {
		const result = projectQuery?.current;
		return result?.ok ? result.data.project : null;
	});
	// `?include=details` puts the FULL task rows on `details`; the bare `tasks` map is ids only.
	const tasks = $derived.by(() => {
		const result = tasksQuery?.current;
		return result?.ok ? (result.data.details ?? []) : [];
	});
</script>

<div class="mx-auto flex w-full max-w-7xl flex-col gap-3 p-4">
	{#if !projectId}
		<Card class="flex flex-col items-start gap-2 px-4 py-6 text-sm">
			<p class="font-medium">Bulk grid needs a labeling task.</p>
			<p class="text-muted-foreground">
				Open one from <a class="underline underline-offset-2" href="{base}/">Labeling tasks</a> and use
				its “Bulk grid” button.
			</p>
		</Card>
	{:else}
		<div class="flex items-center justify-between gap-3">
			<div>
				<h1 class="text-lg font-semibold" data-testid="bulk-title">
					{project?.title || project?.slug || projectId} — bulk grid
				</h1>
				<p class="text-muted-foreground text-sm">
					{tasks.length} item{tasks.length === 1 ? '' : 's'} · annotation state loads per visible row
				</p>
			</div>
			<Button variant="outline" size="sm" href="{base}/tasks/{projectId}">Back to the task</Button>
		</div>
		<BulkGrid {projectId} {tasks} ontology={project?.ontology} />
	{/if}
</div>
