<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { reasonLabel, type PromotionHold } from '#lib/data/promotions.ts';
	import { listPromotions } from '#lib/data/remote/promotions.api.ts';
	import { formatAbsolute } from '@rask/ui/utils';
	import ProjectFilter from '#lib/data/ProjectFilter.svelte';
	import ProjectFilterNotice from '#lib/data/ProjectFilterNotice.svelte';
	import { filterVerdict, projectFilterOf, projectOfTable } from '#lib/data/project-filter.ts';
	import { showProject } from '#lib/data/project-filter-url.ts';
	import { ProjectIndexRead } from '#lib/data/project-index.svelte.ts';

	// The open holds the caller may see, in table order, one page at a time. A hold names its table as
	// the table is named now, so each row links to the table's own hold.
	let holds = $state<PromotionHold[]>([]);
	let nextPage = $state<string | null>(null);
	let status = $state(0);
	let detail = $state('');
	let settled = $state(false);

	// Each hold's project is its table's; the filter narrows the holds this caller was given, a view
	// and never a boundary.
	const projects = new ProjectIndexRead();
	const projectFilter = $derived(projectFilterOf(page.url));
	const chooseProject = (project: string) => void showProject('promotions', page.url, project);
	const allRows = $derived(
		holds.map((hold) => ({
			hold,
			project: projects.index ? projectOfTable(projects.index, hold.table) : null,
		})),
	);
	// What the filter does, from the holds the catalog answered and the projects the caller can see.
	const rowProjects = $derived(new Set(allRows.flatMap((r) => (r.project ? [r.project] : []))));
	const verdict = $derived(
		filterVerdict({
			filter: projectFilter,
			index: projects.state,
			visible: projects.visible.state,
			rowProjects,
		}),
	);
	const rows = $derived(allRows.filter((r) => !projectFilter || r.project === projectFilter));

	async function load(pageToken?: string): Promise<void> {
		const res = await listPromotions(pageToken ? { pageToken } : {});
		settled = true;
		if (!res.ok) {
			status = res.status;
			detail = res.detail;
			return;
		}
		status = 200;
		holds = pageToken ? [...holds, ...res.data.holds] : res.data.holds;
		nextPage = res.data.page_token ?? null;
	}

	onMount(() => {
		void load();
		void projects.load();
	});
</script>

<svelte:head><title>Promotions · rask</title></svelte:head>

<!-- The catalog pages' column: centred, at the lists' width. -->
<div class="mx-auto flex w-full max-w-[860px] flex-col gap-4 px-5 pt-14 pb-10">
	<div>
		<h1 class="text-xl font-semibold">Promotions</h1>
		<p class="text-muted-foreground text-sm">
			A version a project's quality gate found unusual rather than broken waits here for a validator
			instead of being published. Approving moves <code>published</code> to it; rejecting restores
			the table to what <code>published</code> names.
		</p>
	</div>

	<div class="flex items-center gap-2">
		<ProjectFilter
			value={projectFilter}
			visible={projects.visible.ids}
			ofRows={[...rowProjects]}
			onchange={chooseProject}
		/>
	</div>

	{#if !settled}
		<Card class="p-4"><p class="text-muted-foreground text-sm">Loading…</p></Card>
	{:else if status === 401}
		<Card class="p-4"><p class="text-sm">Sign in to see held promotions.</p></Card>
	{:else if status !== 200}
		<Card class="p-4">
			<p class="text-sm">Could not read the held promotions ({status || 'offline'}). {detail}</p>
		</Card>
	{:else if verdict.kind === 'failed' || verdict.kind === 'unseen'}
		<ProjectFilterNotice
			project={projectFilter}
			reason={verdict.kind === 'failed' ? verdict.reason : null}
			onclear={() => chooseProject('')}
		/>
	{:else if verdict.kind === 'waiting'}
		<Card class="p-4"><p class="text-muted-foreground text-sm">Loading…</p></Card>
	{:else if holds.length === 0}
		<Card class="p-4">
			<p class="text-muted-foreground text-sm">
				No promotion visible to you is waiting for a decision.
			</p>
		</Card>
	{:else}
		{#if projectFilter}
			<!-- The holds come a page at a time, and the filter narrows only the pages read so far. -->
			<p class="text-muted-foreground text-sm" data-testid="promotions-filter-scope">
				Among the {holds.length}
				{holds.length === 1 ? 'hold' : 'holds'} loaded{nextPage
					? '; Load more to look further.'
					: ', which are all of them.'}
			</p>
		{/if}
		{#if rows.length === 0}
			<Card class="p-4">
				<p class="text-muted-foreground text-sm">
					No promotion of project <code class="font-mono">{projectFilter}</code> is waiting for a decision.
				</p>
			</Card>
		{:else}
			<Card class="p-0">
				<table class="w-full text-sm" data-testid="promotions">
					<thead class="text-muted-foreground text-left text-xs">
						<tr>
							<th class="p-2">Table</th>
							<th class="p-2">Project</th>
							<th class="p-2">Held</th>
							<th class="p-2">Published</th>
							<th class="p-2">Why</th>
							<th class="p-2">Expires</th>
						</tr>
					</thead>
					<tbody>
						{#each rows as { hold, project } (hold.uuid)}
							<tr class="border-t">
								<td class="p-2">
									<a
										class="font-mono underline"
										href={resolve('/catalog/promotions/[table]', {
											table: encodeURIComponent(hold.table),
										})}>{hold.table}</a
									>
								</td>
								<!-- The dash in the other columns' face: a monospace em dash reads as an en dash. -->
								<td class="p-2">
									{#if project}<span class="font-mono">{project}</span>{:else}<span
											class="text-muted-foreground">—</span
										>{/if}
								</td>
								<td class="p-2"><Badge variant="warning">v{hold.version}</Badge></td>
								<td class="p-2"
									>{hold.published_version === null ? '—' : `v${hold.published_version}`}</td
								>
								<td class="p-2">{hold.reasons.map(reasonLabel).join('; ')}</td>
								<td class="p-2">{formatAbsolute(hold.expires_at)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</Card>
		{/if}
		{#if nextPage}
			<button class="text-sm underline" onclick={() => load(nextPage ?? undefined)}
				>Load more</button
			>
		{/if}
	{/if}
</div>
