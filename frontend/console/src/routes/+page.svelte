<script lang="ts">
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import { Card } from '@riksarkivet/ui/card';
	import { formatZoned } from '@riksarkivet/ui/utils';
	import { Activity, Database, Radio, TriangleAlert } from '@lucide/svelte';
	import DotPattern from '#lib/hero/DotPattern.svelte';
	import MediaBetweenText from '#lib/hero/MediaBetweenText.svelte';
	import { Subject } from '#lib/access/identity/index.ts';
	import ControlEventLine from '#lib/admin/ControlEventLine.svelte';
	import { catalogPath } from '#lib/catalog-trail.ts';
	import { isEstateAdmin } from '#lib/estate-admin.ts';
	import { recentCommits, tablesByProject } from '#lib/home.ts';
	import { projectOf } from '#lib/data/project-filter.ts';
	import { ProjectIndexRead } from '#lib/data/project-index.svelte.ts';
	import { fetchTables } from '#lib/data/remote/catalog.api.ts';
	import { fetchDlq, fetchRecentEvents } from '#lib/lineage/remote/lineage.api.ts';
	import { controlFeed } from '#lib/live/feeds.api.ts';

	// HOME — what this estate is and what is happening in it, for the person signed in. Every figure is
	// a read the doors already serve the caller, so it is the caller's own view: the tables their grants
	// list and the commits lineage shows them. An estate admin also sees the dead letters and the latest
	// governance changes, which the doors refuse everyone else.

	/** How many of the newest commits and governance changes the page lists. */
	const RECENT = 8;

	const me = $derived(page.data.me);
	const admin = $derived(isEstateAdmin(me));

	const projects = new ProjectIndexRead();
	void projects.load();

	const tables = fetchTables();
	const events = fetchRecentEvents(RECENT);
	// Only an estate admin may read these two, and creating a read sends it, so nobody else asks.
	// One event is enough: the backlog's `depth` counts every one.
	const dlq = $derived(admin ? fetchDlq(1) : null);
	const dlqRead = $derived(dlq?.current);
	const governance = $derived(admin ? controlFeed() : null);

	const perProject = $derived.by(() => {
		const listed = tables.current;
		const index = projects.index;
		if (!listed?.ok || index === null) return null;
		return tablesByProject(listed.data.tables, (id) => projectOf(index, id));
	});
	const commits = $derived(events.current ? recentCommits(events.current.events) : null);
	const governanceEntries = $derived(
		(governance?.current?.entries ?? []).filter((e) => e.kind === 'event').slice(0, RECENT),
	);

	/** Why a read failed, in words: unreachable, or the door's status and its own detail. */
	const failed = (res: { status: number; detail: string }): string =>
		res.status === 0 ? 'the catalog could not be reached' : `HTTP ${res.status}: ${res.detail}`;
</script>

<svelte:head><title>Home · rask</title></svelte:head>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-6 p-6">
	<!-- `isolate` gives the dot field a stacking context of its own, so the decoration cannot paint over
	     anything outside this panel. -->
	<section class="border-border bg-card relative isolate overflow-hidden rounded-xl border">
		<DotPattern
			glow
			cr={0.9}
			width={22}
			height={22}
			class="text-foreground/[0.07] dark:text-foreground/[0.10] mask-[radial-gradient(420px_circle_at_center,white,transparent)]"
		/>
		<div class="relative flex flex-col items-center gap-4 px-6 py-10 text-center">
			<h1 class="sr-only">Home</h1>
			<p class="text-5xl font-semibold tracking-tight sm:text-6xl" data-slot="home-wordmark">
				<!-- alt is EMPTY on purpose: the image is a flourish inside a wordmark, and any alt text
				     would land in the middle of the wordmark's text. -->
				<MediaBetweenText
					firstText="LA"
					secondText="GOM"
					mediaSrc="/peepo-justright.png"
					alt=""
					targetWidth={58}
				/>
			</p>
			<p class="text-muted-foreground max-w-2xl text-sm">
				The governed lakehouse: every table here is written and read through the catalog, on any
				compute, with lineage and grants across all of it.
			</p>
			{#if me}
				<p class="text-sm" data-testid="home-signed-in">
					Signed in as <span class="font-medium">{me.name ?? me.email ?? me.sub}</span>
				</p>
			{/if}
		</div>
	</section>

	<div class="grid gap-4 md:grid-cols-2">
		<Card class="flex flex-col gap-3 p-5 md:col-span-2" data-testid="home-tables">
			<div class="flex items-center gap-2">
				<Database class="text-muted-foreground size-4" aria-hidden="true" />
				<h2 class="font-medium">Tables</h2>
				<a href={resolve('/catalog/tables')} class="text-muted-foreground ml-auto text-xs underline"
					>All tables</a
				>
			</div>
			{#if tables.current && !tables.current.ok}
				<p class="text-muted-foreground text-sm">
					The tables could not be read: {failed(tables.current)}.
				</p>
			{:else if projects.state.kind === 'failed'}
				<p class="text-muted-foreground text-sm">
					The tables could not be grouped by project: {projects.state.reason}.
				</p>
			{:else if perProject === null}
				<p class="text-muted-foreground text-sm">Loading…</p>
			{:else if perProject.length === 0}
				<p class="text-muted-foreground text-sm">No table your grants list yet.</p>
			{:else}
				<table class="w-full text-sm">
					<thead class="text-muted-foreground text-left text-xs">
						<tr>
							<th class="py-1 font-normal">project</th>
							<th class="py-1 text-right font-normal">tables</th>
						</tr>
					</thead>
					<tbody>
						{#each perProject as row (row.project ?? '')}
							<tr class="border-border border-t">
								<td class="py-1.5">
									{#if row.project}
										<a
											class="underline-offset-2 hover:underline"
											href={resolve(`catalog/tables?project=${encodeURIComponent(row.project)}`)}
											>{row.project}</a
										>
									{:else}
										<span class="text-muted-foreground">the estate’s own</span>
									{/if}
								</td>
								<td class="py-1.5 text-right font-medium tabular-nums">{row.total}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</Card>

		<Card
			class={`flex flex-col gap-3 p-5 ${admin ? '' : 'md:col-span-2'}`}
			data-testid="home-commits"
		>
			<div class="flex items-center gap-2">
				<Activity class="text-muted-foreground size-4" aria-hidden="true" />
				<h2 class="font-medium">Recent commits</h2>
			</div>
			{#if commits === null}
				<p class="text-muted-foreground text-sm">
					{events.loading ? 'Loading…' : 'Lineage’s feed could not be read.'}
				</p>
			{:else if commits.length === 0}
				<p class="text-muted-foreground text-sm">No commit lineage shows you yet.</p>
			{:else}
				<ul class="flex flex-col gap-2 text-sm">
					{#each commits as commit (commit.seq)}
						<li class="flex flex-col gap-0.5">
							<span class="flex flex-wrap items-baseline gap-x-2">
								<span class="font-medium">{commit.operation.replaceAll('_', ' ')}</span>
								{#each commit.tables as table (table)}
									<a
										class="underline-offset-2 hover:underline"
										href={resolve('/catalog/tables/[table]', { table: encodeURIComponent(table) })}
										>{catalogPath(table)}</a
									>
								{/each}
							</span>
							<span class="text-muted-foreground flex flex-wrap items-center gap-x-2 text-xs">
								{#if commit.at}<span>{formatZoned(commit.at)}</span>{/if}
								<Subject value={commit.author} />
							</span>
						</li>
					{/each}
				</ul>
			{/if}
		</Card>

		{#if admin}
			<Card class="flex flex-col gap-3 p-5" data-testid="home-attention">
				<div class="flex items-center gap-2">
					<TriangleAlert class="text-muted-foreground size-4" aria-hidden="true" />
					<h2 class="font-medium">Needs attention</h2>
				</div>
				<dl class="flex flex-col gap-2 text-sm">
					<div class="flex items-baseline gap-2">
						<dt>
							<a class="underline-offset-2 hover:underline" href={resolve('/admin/dlq')}
								>Lineage events not yet delivered</a
							>
						</dt>
						<dd class="ml-auto font-medium tabular-nums" data-testid="home-dlq">
							{#if dlqRead?.ok}
								{dlqRead.data.depth}
							{:else if dlqRead}
								<span class="text-muted-foreground font-normal"
									>unavailable ({failed(dlqRead)})</span
								>
							{:else}…{/if}
						</dd>
					</div>
				</dl>
			</Card>

			<Card class="flex flex-col gap-3 p-5 md:col-span-2" data-testid="home-governance">
				<div class="flex items-center gap-2">
					<Radio class="text-muted-foreground size-4" aria-hidden="true" />
					<h2 class="font-medium">Governance changes</h2>
					<a href={resolve('/admin/events')} class="text-muted-foreground ml-auto text-xs underline"
						>All events</a
					>
				</div>
				{#if governance?.current?.failure}
					<p class="text-muted-foreground text-sm">
						The governance feed could not be read: {failed(governance.current.failure)}.
					</p>
				{:else if governance?.current === undefined}
					<p class="text-muted-foreground text-sm">Loading…</p>
				{:else if governanceEntries.length === 0}
					<p class="text-muted-foreground text-sm">
						No governance change since the catalog started.
					</p>
				{:else}
					<ul class="flex flex-col gap-1 text-sm">
						{#each governanceEntries as entry (entry.key)}
							{#if entry.kind === 'event'}<ControlEventLine event={entry.event} />{/if}
						{/each}
					</ul>
				{/if}
			</Card>
		{/if}
	</div>
</div>
