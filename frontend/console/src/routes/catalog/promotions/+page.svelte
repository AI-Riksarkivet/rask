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
	import { projectFilterOf, projectOfTable } from '#lib/data/project-filter.ts';
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
	const rows = $derived(
		holds
			.map((hold) => ({
				hold,
				project: projects.index ? projectOfTable(projects.index, hold.table) : null,
			}))
			.filter((r) => !projectFilter || r.project === projectFilter),
	);

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

<div class="flex flex-col gap-4 p-4">
	<div>
		<h1 class="text-lg font-semibold">Promotions</h1>
		<p class="text-muted-foreground text-sm">
			A version a project's quality gate found unusual rather than broken waits here for a validator
			instead of being published. Approving moves <code>published</code> to it; rejecting restores
			the table to what <code>published</code> names.
		</p>
	</div>

	<div class="flex items-center gap-2">
		<ProjectFilter value={projectFilter} onchange={chooseProject} />
	</div>

	{#if !settled}
		<Card class="p-4"><p class="text-muted-foreground text-sm">Loading…</p></Card>
	{:else if status === 401}
		<Card class="p-4"><p class="text-sm">Sign in to see held promotions.</p></Card>
	{:else if status !== 200}
		<Card class="p-4">
			<p class="text-sm">Could not read the held promotions ({status || 'offline'}). {detail}</p>
		</Card>
	{:else if holds.length === 0}
		<Card class="p-4">
			<p class="text-muted-foreground text-sm">
				No promotion visible to you is waiting for a decision.
			</p>
		</Card>
	{:else if projectFilter && projects.state.kind === 'failed'}
		<ProjectFilterNotice
			project={projectFilter}
			reason={projects.state.reason}
			onclear={() => chooseProject('')}
		/>
	{:else if projectFilter && projects.state.kind === 'loading'}
		<Card class="p-4"><p class="text-muted-foreground text-sm">Loading…</p></Card>
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
								<td class="p-2 font-mono">{project ?? '—'}</td>
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
