<script lang="ts">
	import { Bell, BellOff, Loader } from '@lucide/svelte';
	import { Card } from '@rask/ui/card';
	import { Switch } from '@rask/ui/switch';
	import { onMount } from 'svelte';
	import { readWatches, unwatchProject, watchProject } from '$lib/live/inbox.remote';
	import { getProjects } from '$lib/remote/home.remote';

	// PROJECT WATCHES — v2 targeting's own surface (open_notifications.md S4).
	//
	// NOT under `/settings`, and that is the load-bearing decision on this page. `/settings/**` is
	// estate-admin only and its layout gate 404s everyone else, because it configures the PLATFORM. A
	// watch is the opposite kind of thing: a per-user preference about what interrupts YOU, and every
	// signed-in user has to be able to reach their own. Putting it behind the admin door would mean the
	// only people who could manage their notifications are the people least likely to need to.
	//
	// The estate's standing rule is visible in the UI, not just in the service: membership GATES
	// watching and never implies it. So nothing here is on by default, and the list of projects you may
	// watch is exactly the list you are a member of — the service checks `project#member` again on the
	// write, and this page is presentation over that check rather than a substitute for it.
	//
	// Every action is SHOWN, never hidden: a project you cannot watch would render disabled with the
	// reason, not omitted. Today there is no such case on this page — `getProjects` already answers
	// with what you can see — so the shape is stated rather than exercised.

	// `null` = un-wired (no session, or no notifications service). Genuinely different from an empty
	// list, which means you watch nothing, and the page says which.
	let watches = $state<string[] | null>(null);
	let loaded = $state(false);
	let projects = $state<{ id: string; name?: string }[]>([]);
	// Per-project in-flight, so one slow write does not freeze every other row's control. A plain
	// array rather than a Set: it is replaced wholesale on every change (never mutated in place), which
	// is what keeps it reactive without reaching for SvelteSet for a list that is at most a few rows.
	let pending = $state<string[]>([]);

	onMount(() => {
		void Promise.all([readWatches(), getProjects()])
			.then(([w, p]) => {
				watches = w;
				// A project is keyed by its SLUG estate-wide — the id the catalog and FGA both name it by.
				projects = (p ?? []).map((project) => ({ id: project.slug, name: project.name }));
			})
			.catch(() => {
				/* un-wired stays un-wired; the empty state below says so */
			})
			.finally(() => {
				loaded = true;
			});
	});

	const watching = $derived(new Set(watches ?? []));

	async function toggle(projectId: string, next: boolean) {
		pending = [...pending, projectId];
		try {
			// Optimistic, then reconciled from the server's own answer: the badge is the service's
			// count, and a control that moved without the write landing is a control that lies.
			const ok = next ? await watchProject(projectId) : await unwatchProject(projectId);
			if (ok) watches = await readWatches();
		} catch {
			/* leave the switch where the server has it — the next read repairs the row */
		} finally {
			pending = pending.filter((id) => id !== projectId);
		}
	}
</script>

<svelte:head><title>Notifications — rask</title></svelte:head>

<div class="mx-auto w-full max-w-3xl px-6 py-8">
	<header class="mb-6">
		<h1 class="flex items-center gap-2 text-xl font-semibold">
			<Bell class="size-5" aria-hidden="true" />
			Notifications
		</h1>
		<p class="text-muted-foreground mt-1 text-sm">
			You are always told about runs you started. Watch a project to also be told about everyone else's
			runs in it — nothing is watched by default.
		</p>
	</header>

	{#if !loaded}
		<p class="text-muted-foreground flex items-center gap-2 py-8 text-sm">
			<Loader class="size-4 animate-spin" aria-hidden="true" />
			Reading your watches…
		</p>
	{:else if watches === null}
		<!-- The un-wired case, said out loud rather than rendered as "you watch nothing". -->
		<Card class="p-6 text-center">
			<BellOff class="text-muted-foreground mx-auto mb-2 size-5" aria-hidden="true" />
			<p class="text-sm font-medium">Watching is unavailable on this stack.</p>
			<p class="text-muted-foreground mt-1 text-xs">
				Either you are not signed in, or the notification service is not reachable from this zone. Runs
				you start still reach your bell.
			</p>
		</Card>
	{:else if projects.length === 0}
		<Card class="p-6 text-center">
			<p class="text-sm font-medium">You are not a member of any project yet.</p>
			<p class="text-muted-foreground mt-1 text-xs">
				Project membership is what makes a project watchable — it gates watching, and never implies it.
			</p>
		</Card>
	{:else}
		<ul class="divide-border/60 divide-y rounded-lg border">
			{#each projects as project (project.id)}
				{@const on = watching.has(project.id)}
				{@const busy = pending.includes(project.id)}
				<li
					class="flex items-center gap-4 px-4 py-3"
					data-slot="watch-row"
					data-project={project.id}
				>
					<div class="min-w-0 flex-1">
						<p class="truncate text-sm leading-tight font-medium">{project.name ?? project.id}</p>
						<p class="text-muted-foreground mt-0.5 truncate text-xs">
							{on ? 'You are told about every run in this project' : 'Only your own runs'}
						</p>
					</div>
					<Switch
						checked={on}
						disabled={busy}
						aria-label={`Watch ${project.name ?? project.id}`}
						onCheckedChange={(next) => toggle(project.id, next)}
					/>
				</li>
			{/each}
		</ul>
		<p class="text-muted-foreground mt-4 text-xs">
			Watching is re-checked when a run finishes: if you leave a project, its runs stop reaching you
			even while the switch is still on.
		</p>
	{/if}
</div>
