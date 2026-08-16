<script lang="ts">
	// `/lakehouse` — the zone's OVERVIEW of the ACTIVE PROJECT (#109).
	//
	// This route used to 307 straight into `/lakehouse/catalog`, so opening the lakehouse dumped you
	// into one area's table list with nothing saying which project you were standing in. Every other
	// zone that has a landing lands on its SUBJECT — compute on its cluster — and the lakehouse's
	// subject is the project the estate put you inside (#103's `rask_active_project` cookie, surfaced
	// on every zone's layout data by `zoneLayoutLoad`). The catalog is unchanged and still one click
	// away as its own area; it simply stopped being the thing the zone root pretends to be.
	//
	// The hierarchy VIEW is `@rask/ui/hierarchy` — the same component the home zone's `/projects/<id>`
	// renders, hoisted there because zones may not import each other. It owns no transport: this zone
	// hands it its OWN remote functions, so every rung is read with this user's bearer against the
	// catalog's own gate, and a rung the caller cannot read draws as "unreadable" rather than absent.
	import { FolderKanban, RefreshCw, ShieldAlert, Warehouse as WarehouseIcon } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { ProjectHierarchy, type HierarchyHref } from '@rask/ui/hierarchy';
	import type { WarehouseRecord } from '$lib/data/catalog';
	import { fetchNamespaceTables } from '$lib/data/remote/namespace.remote';
	import { fetchWarehouseNamespaces, fetchWarehouses } from '$lib/data/remote/warehouses.remote';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
	const LH = base;
	// TODO 4a: the graph is a MAP. Every rung links to the page that owns that object, and the URLs are
	// built HERE because they are this estate's routes — `@rask/ui` is mounted by two zones with
	// different bases and may not assert either one's routing.
	//
	// `project` returns null deliberately: from a project view, the project rung IS this page.
	const rungHref: HierarchyHref = (rung) => {
		if (rung.tier === 'project') return null;
		if (rung.tier === 'warehouse') return `${LH}/catalog/warehouses/${encodeURIComponent(rung.id)}`;
		if (rung.tier === 'namespace') return `${LH}/catalog/namespaces/${encodeURIComponent(rung.id)}`;
		return `${LH}/catalog/tables/${encodeURIComponent(rung.id)}`;
	};


	// `zoneLayoutLoad` defaults the cookie to '' — an empty string is "no project is open", which is a
	// real state of the estate and not a loading one.
	const project = $derived(data.activeProject);

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	// The registry read is estate-wide (`GET /v1/warehouses` shows the caller what they may see) and
	// filtered to the active project HERE. The per-project catalog read deliberately does not live in
	// this zone — `fetchProject` moved to home with the project surface — and re-adding it would put a
	// second project reader in a zone whose whole remit is what sits BELOW a project.
	let warehouses = $state<WarehouseRecord[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);

	const unauthorized = $derived(settled && lastStatus === 401);
	const denied = $derived(settled && lastStatus === 403);
	const offline = $derived(settled && ![200, 401, 403].includes(lastStatus));

	const mine = $derived((warehouses ?? []).filter((w) => w.project === project));

	async function load(): Promise<void> {
		const current = project;
		const res = await fetchWarehouses();
		if (project !== current) return; // latest-wins across navigation
		settled = true;
		warehouses = res.ok ? res.data : null;
		lastStatus = res.ok ? 200 : res.status;
	}

	$effect(() => {
		if (!project) return;
		void project;
		warehouses = null;
		lastStatus = 0;
		settled = false;
		void load();
	});

	function statusOf(w: WarehouseRecord): string {
		return w.status ?? 'active';
	}
</script>

<svelte:head><title>{project || 'Overview'} · lakehouse · lance</title></svelte:head>

<div class="mx-auto w-full max-w-5xl px-5 pt-14 pb-10">
	{#if !project}
		<!-- No cookie, no pretending — but the scope claimed here is THIS PAGE's, never the zone's.

		     It read "Every lakehouse surface is scoped to the project you are standing in — warehouses,
		     namespaces and tables all hang beneath one", and that was false. Checked in the code rather
		     than assumed: `fetchTables` calls `/v1/table`, `fetchNamespaceTables` calls
		     `/v1/namespace/<ns>/table/list`, the lineage remotes name no project at all, and
		     `catalogJSON` ($lib/server/doors.ts) forwards the session bearer and NOTHING else — no
		     project header, no project query. Those surfaces are estate-wide reads filtered by the
		     caller's own FGA grants, and they render fine with no project open. The navbar's Lakehouse
		     trigger points straight at one of them.

		     The sentence cost something real: it was read as a specification, and the rail was changed to
		     disable every project-scoped entry — greying out working pages and the one-click path to
		     tables. That was reverted; the sentence was the thing that was wrong.

		     What IS per-project is this overview: the hierarchy, and the warehouses whose `project` field
		     claims this one (`mine`, below). So the page now says that about itself, and still points at
		     the estate's project list. The link leaves this zone for the catch-all's `/projects`, so it
		     MUST hard-navigate. -->
		<header class="mb-4 flex items-center gap-2">
			<FolderKanban size={15} class="text-muted-foreground" />
			<h1 class="text-xl font-medium">Lakehouse</h1>
		</header>
		<div class="bg-card rounded-lg border px-5 py-8">
			<p class="mb-1 text-sm font-medium">No active project.</p>
			<p class="text-muted-foreground max-w-[62ch] text-sm">
				This overview is per project — a project's hierarchy, and the warehouses claiming it. Open one
				and this page becomes its summary. The rest of the zone does not need one: Catalog and Lineage
				list every table, namespace and run your grants allow, across the estate.
			</p>
			<p class="mt-4 text-sm">
				<a class="underline underline-offset-4" href="/projects" data-sveltekit-reload>
					Open one from Projects →
				</a>
			</p>
		</div>
	{:else}
		<header class="mb-1 flex items-center gap-2">
			<FolderKanban size={15} class="text-muted-foreground" />
			<h1 class="font-mono text-xl font-medium">{project}</h1>
		</header>
		<p class="text-muted-foreground mb-6 text-xs">
			The active project, seen from the lakehouse — its hierarchy and the warehouses that claim it.
			Only this page is per project: Catalog and Lineage list every table, namespace and run your
			grants allow, across the estate.
			<!-- UP a rung and ACROSS the zone seam: the project record itself is the home zone's.
			     data-sveltekit-reload is mandatory — a soft nav would resolve against this zone's route
			     manifest, which owns no /projects route, and 404. -->
			<a
				class="text-foreground underline underline-offset-4"
				href={`/projects/${encodeURIComponent(project)}`}
				data-sveltekit-reload>Project settings and admins →</a
			>
		</p>

		{#if unauthorized}
			<div class="text-muted-foreground flex items-center gap-2 py-8">
				<ShieldAlert size={16} />
				<p class="text-sm">
					This stack is governed — <a
						class="underline underline-offset-4"
						href={loginHref}
						data-sveltekit-reload>sign in</a
					> to see this project's warehouses.
				</p>
			</div>
		{:else if denied}
			<div class="text-muted-foreground flex items-center gap-2 py-8">
				<ShieldAlert size={16} />
				<p class="text-sm">You don't have registry access to this project's warehouses.</p>
			</div>
		{:else if offline}
			<div class="text-muted-foreground flex items-center gap-2 py-8">
				<RefreshCw size={16} />
				<!-- status 0 is an unreachable catalog, where no server answered at all — printing
				     "HTTP 0" would name a status nothing sent. -->
				<p class="text-sm">Catalog unreachable{lastStatus === 0 ? '' : ` (HTTP ${lastStatus})`}.</p>
			</div>
		{:else if warehouses === null}
			<p class="text-muted-foreground py-8 text-sm">Loading…</p>
		{:else}
			<section class="mb-7">
				<h2 class="mb-2 text-sm font-medium">Hierarchy</h2>
				<ProjectHierarchy
					{project}
					warehouses={mine}
					fetchNamespaces={fetchWarehouseNamespaces}
					fetchTables={fetchNamespaceTables}
					hrefFor={rungHref}
				/>
			</section>

			<section class="mb-7">
				<h2 class="mb-2 text-sm font-medium">
					Warehouses
					<span class="text-muted-foreground font-normal">({mine.length})</span>
				</h2>
				{#if mine.length === 0}
					<p class="text-muted-foreground text-xs">
						No warehouses claim <code class="font-mono">{project}</code> yet — the first one is
						provisioned from
						<a
							class="text-foreground underline underline-offset-4"
							href={`/projects/${encodeURIComponent(project)}`}
							data-sveltekit-reload>the project page</a
						>.
					</p>
				{:else}
					<div class="overflow-x-auto">
						<table class="w-full border-collapse text-xs">
							<thead>
								<tr class="text-muted-foreground border-b text-left">
									<th class="py-1 pr-4 font-medium">warehouse</th>
									<th class="py-1 pr-4 font-medium">bucket</th>
									<th class="py-1 pr-4 font-medium">serving</th>
									<th class="py-1 font-medium">status</th>
								</tr>
							</thead>
							<tbody>
								{#each mine as w (w.id)}
									<tr class="border-border/45 border-b">
										<td class="py-1.5 pr-4">
											<!-- DOWN a rung, same zone: the warehouse page is this zone's own, so it
											     stays a soft nav written against {base}. -->
											<a
												class="font-mono underline-offset-4 hover:underline"
												href={`${base}/catalog/warehouses/${encodeURIComponent(w.id)}`}
											>
												<WarehouseIcon size={12} class="inline align-[-1px]" />
												{w.id}
											</a>
										</td>
										<td class="text-muted-foreground py-1.5 pr-4 font-mono">{w.bucket}</td>
										<td class="text-muted-foreground py-1.5 pr-4 font-mono">{w.serving ?? '—'}</td>
										<td class="py-1.5 font-mono">
											<span
												class="rounded-sm border px-1.5 py-0.5"
												class:border-success={statusOf(w) === 'active'}
												class:border-warning={statusOf(w) !== 'active'}>{statusOf(w)}</span
											>
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			</section>
		{/if}
	{/if}
</div>
