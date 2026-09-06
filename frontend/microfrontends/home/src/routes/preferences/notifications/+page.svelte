<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { Input } from '@rask/ui/input';
	import { Switch } from '@rask/ui/switch';
	import { Bell, Eye, Mail, MessageSquare } from '@lucide/svelte';
	import { onMount } from 'svelte';
	import type { ChannelPreferences } from '@rask/api/inbox';
	import { readPrefs, readWatches, savePrefs, unwatchProject } from '$lib/live/inbox.remote';

	// NOTIFICATIONS — the per-subject half of the notification plane (`GET|PUT /prefs` and `/watches`). The bell tells you what happened; this decides whether
	// anything ALSO reaches you when the tab is shut.
	//
	// WHY `/preferences` AND NOT `/settings`. It shipped under `/settings/notifications` for one
	// commit and that was wrong: `settings/+layout.server.ts` 404s any non-admin — deliberately, and
	// as a LAYOUT load so the door cannot be forgotten per child — while channel prefs and project
	// watches are per SUBJECT. The feature belongs to everyone and the door admitted almost nobody.
	//
	// Relaxing the guard for one child was the other option and it is the worse one: that file's own
	// comment is that a door which has to be remembered per child is a door that will be forgotten.
	// So the page moved instead. `/settings` keeps only what configures the INSTALLATION; anything
	// scoped to you lives here and is reached from the account menu, which every signed-in user has.
	//
	// WHAT IS DELIBERATELY NOT HERE: the SMTP host, the Slack webhook and their credentials. Those are
	// Dapr Component config, and the settings index already states the rule for that class — secrets
	// are "never editable from a browser form". A channel appears here when an operator mints its
	// Component; this page chooses whether YOU use it and where it goes.

	/** The channels this page knows how to LABEL. The wire list is open — the service's channel table
	 *  is a dict and the chart mints one Component per destination — so anything unrecognised is still
	 *  rendered, by id, rather than dropped. A channel the UI has never heard of must not become a
	 *  preference the user cannot see they have. */
	const KNOWN: Record<string, { label: string; hint: string; icon: typeof Mail }> = {
		email: { label: 'Email', hint: 'Where terminal runs land in your inbox.', icon: Mail },
		slack: {
			label: 'Slack',
			hint: 'A webhook URL, or the channel your operator configured.',
			icon: MessageSquare,
		},
	};

	let prefs = $state<ChannelPreferences | null>(null);
	let watches = $state<string[] | null>(null);
	let loaded = $state(false);
	let saving = $state(false);
	let saveError = $state(false);

	// Read on MOUNT, never during render: a settings page must not hold the server on the notification
	// plane's first answer — the same rule the bell's feed follows in every zone's root layout.
	onMount(() => {
		void Promise.all([readPrefs(), readWatches()])
			.then(([p, w]) => {
				prefs = p;
				watches = w;
			})
			.catch(() => {
				/* both stay null — rendered as unavailable, which is the honest reading of a failed read */
			})
			.finally(() => {
				loaded = true;
			});
	});

	/** Every channel to render: the ones we know, plus any the server already has an opinion about. */
	const channelIds = $derived([
		...new Set([
			...Object.keys(KNOWN),
			...(prefs?.channels ?? []),
			...Object.keys(prefs?.destinations ?? {}),
		]),
	]);

	const enabled = (id: string) => prefs?.channels.includes(id) ?? false;
	const digestOn = $derived(prefs?.digest_seconds !== null && prefs?.digest_seconds !== undefined);

	function toggle(id: string, on: boolean) {
		if (!prefs) return;
		const channels = on
			? [...new Set([...prefs.channels, id])]
			: prefs.channels.filter((c) => c !== id);
		prefs = { ...prefs, channels };
	}

	function setDestination(id: string, value: string) {
		if (!prefs) return;
		prefs = { ...prefs, destinations: { ...prefs.destinations, [id]: value } };
	}

	function setDigest(on: boolean) {
		if (!prefs) return;
		// 900 s is a starting point, not a ruling — the service's own floor is 60 (`ge=60`).
		prefs = { ...prefs, digest_seconds: on ? (prefs.digest_seconds ?? 900) : null };
	}

	async function save() {
		if (!prefs) return;
		saving = true;
		saveError = false;
		const saved = await savePrefs(prefs).catch(() => null);
		// A refused write leaves the form as the user left it and SAYS so. It does not silently revert:
		// re-rendering the server's older document over what someone just typed reads as data loss.
		if (saved) prefs = saved;
		else saveError = true;
		saving = false;
	}

	async function stopWatching(projectId: string) {
		const ok = await unwatchProject(projectId).catch(() => false);
		if (ok) watches = (watches ?? []).filter((p) => p !== projectId);
	}
</script>

<svelte:head><title>Notifications · Settings · rask</title></svelte:head>

<div class="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
	<header class="flex flex-col gap-1">
		<h1 class="flex items-center gap-2 text-2xl font-semibold">
			<Bell class="size-5" aria-hidden="true" />
			Notifications
		</h1>
		<p class="text-muted-foreground text-sm">
			The bell always shows what concerns you. These decide whether anything also reaches you when
			this tab is closed.
		</p>
	</header>

	{#if !loaded}
		<p class="text-muted-foreground text-sm">Reading your preferences…</p>
	{:else if !prefs}
		<!-- Unavailable is a STATE, not an empty form. A form that cannot save is worse than an absent
		     one — the settings index says exactly this about its unwired rows. -->
		<Card class="flex items-center gap-3 p-4">
			<Bell class="text-muted-foreground size-5 shrink-0" aria-hidden="true" />
			<span class="flex min-w-0 flex-col">
				<span class="font-medium">Channels are unavailable on this stack</span>
				<span class="text-muted-foreground text-xs">
					No signed-in session, or the notifications service is not reachable. The bell still works
					— it falls back to per-tab memory.
				</span>
			</span>
			<Badge variant="outline" class="ml-auto shrink-0">Not available</Badge>
		</Card>
	{:else}
		<section class="flex flex-col gap-2" aria-labelledby="channels">
			<h2 id="channels" class="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
				Channels
			</h2>
			<p class="text-muted-foreground text-xs">
				A channel appears here once an operator has configured its component. Hosts, webhook URLs
				and credentials are never editable from a browser.
			</p>
			{#each channelIds as id (id)}
				{@const known = KNOWN[id]}
				{@const Icon = known?.icon ?? Bell}
				<Card class="flex flex-col gap-3 p-4">
					<div class="flex items-center gap-3">
						<Icon class="text-muted-foreground size-5 shrink-0" aria-hidden="true" />
						<span class="flex min-w-0 flex-col">
							<span class="font-medium">{known?.label ?? id}</span>
							<span class="text-muted-foreground text-xs">
								{known?.hint ?? 'A channel this installation configured.'}
							</span>
						</span>
						<Switch
							class="ml-auto shrink-0"
							checked={enabled(id)}
							aria-label={`Enable ${known?.label ?? id}`}
							onCheckedChange={(on) => toggle(id, on)}
						/>
					</div>
					{#if enabled(id)}
						<label class="flex flex-col gap-1 text-xs">
							<span class="text-muted-foreground">Destination</span>
							<Input
								value={prefs.destinations[id] ?? ''}
								placeholder={id === 'email' ? 'you@example.org' : 'https://hooks.slack.com/…'}
								oninput={(e) => setDestination(id, e.currentTarget.value)}
							/>
						</label>
					{/if}
				</Card>
			{/each}
		</section>

		<section class="flex flex-col gap-2" aria-labelledby="digest">
			<h2 id="digest" class="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
				Batching
			</h2>
			<Card class="flex items-center gap-3 p-4">
				<span class="flex min-w-0 flex-col">
					<span class="font-medium">Batch pushes into a digest</span>
					<span class="text-muted-foreground text-xs">
						Off means immediate, which is the default. A failed run is never batched whatever this
						says.
					</span>
				</span>
				<Switch
					class="ml-auto shrink-0"
					checked={digestOn}
					aria-label="Batch pushes into a digest"
					onCheckedChange={setDigest}
				/>
			</Card>
			{#if digestOn}
				<label class="flex flex-col gap-1 text-xs">
					<span class="text-muted-foreground">Digest interval (seconds, minimum 60)</span>
					<Input
						type="number"
						min="60"
						value={String(prefs.digest_seconds ?? 900)}
						oninput={(e) => {
							const n = Number(e.currentTarget.value);
							if (prefs && Number.isFinite(n))
								prefs = { ...prefs, digest_seconds: Math.max(60, n) };
						}}
					/>
				</label>
			{/if}
		</section>

		<div class="flex items-center gap-3">
			<Button onclick={save} disabled={saving}>{saving ? 'Saving…' : 'Save preferences'}</Button>
			{#if saveError}
				<span class="text-destructive text-xs">
					That did not save — your changes are still here. Try again.
				</span>
			{/if}
		</div>
	{/if}

	<section class="flex flex-col gap-2" aria-labelledby="watches">
		<h2 id="watches" class="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
			Watched projects
		</h2>
		<p class="text-muted-foreground text-xs">
			Watching a project tells you about its runs even when you did not start them. Watching is
			added from a project's own page; membership is what allows it.
		</p>
		{#if watches === null}
			<p class="text-muted-foreground text-sm">Watches are unavailable on this stack.</p>
		{:else if watches.length === 0}
			<p class="text-muted-foreground text-sm">
				You watch nothing — you are told about your own work only.
			</p>
		{:else}
			<ul class="flex flex-col gap-2">
				{#each watches as project (project)}
					<li class="flex items-center gap-3 rounded-lg border p-3">
						<Eye class="text-muted-foreground size-4 shrink-0" aria-hidden="true" />
						<span class="min-w-0 truncate text-sm">{project}</span>
						<Button
							variant="ghost"
							size="xs"
							class="ml-auto shrink-0"
							onclick={() => stopWatching(project)}
						>
							Stop watching
						</Button>
					</li>
				{/each}
			</ul>
		{/if}
	</section>
</div>
