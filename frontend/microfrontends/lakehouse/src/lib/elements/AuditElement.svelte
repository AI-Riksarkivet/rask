<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/** `<rask-lakehouse-audit>` — the governance audit trail, served by the lakehouse zone to the
	 *  global workbench. Reads the zone's own /lakehouse/api/audit BFF (root-absolute, session
	 *  riding); the shape mirrors AuditViewer's structural type. */
	import { ElementPoll } from './poll.svelte';

	type AuditEvent = {
		timestamp: string;
		action: string;
		outcome: string;
		subject: string;
		resource: string;
	};

	let { pollms = 30000 }: { pollms?: number } = $props();
	const poll = new ElementPoll<AuditEvent[]>(async (f) => {
		const res = await f('/lakehouse/api/audit?limit=100');
		if (res.status === 401) throw new Error('session expired or no access');
		if (!res.ok) throw new Error(`HTTP ${res.status}`);
		const body = (await res.json()) as { events?: AuditEvent[] };
		return body.events ?? [];
	});
	$effect(() => poll.start(pollms));

	const events = $derived(poll.data ?? []);
</script>

<div class="ce-panel">
	<p class="meta">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if poll.error !== null}
		<p class="error">Audit trail unavailable: {poll.error}</p>
	{:else if events.length === 0}
		<p class="empty">No audit events in the window.</p>
	{:else}
		<table>
			<thead>
				<tr><th>Time</th><th>Action</th><th>Outcome</th><th>Subject</th><th>Resource</th></tr>
			</thead>
			<tbody>
				{#each events as ev, i (i)}
					<tr>
						<td class="dim">{ev.timestamp}</td>
						<td class="mono">{ev.action}</td>
						<td
							><span class="pill" data-ok={ev.outcome === 'allow' || ev.outcome === 'ok'}
								>{ev.outcome}</span
							></td
						>
						<td class="mono">{ev.subject}</td>
						<td class="mono dim">{ev.resource}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>

<style>
	.ce-panel {
		display: block;
		height: 100%;
		overflow: auto;
		padding: 0.75rem;
		color: var(--color-foreground);
		font-size: 0.8125rem;
	}
	.meta {
		margin: 0 0 0.5rem;
		color: var(--color-muted-foreground);
		font-size: 0.6875rem;
	}
	.error {
		color: var(--color-destructive);
	}
	.empty {
		color: var(--color-muted-foreground);
	}
	table {
		width: 100%;
		border-collapse: collapse;
	}
	th {
		text-align: left;
		font-weight: 500;
		color: var(--color-muted-foreground);
		border-bottom: 1px solid var(--color-border);
		padding: 0.25rem 0.5rem;
	}
	td {
		border-bottom: 1px solid var(--color-border);
		padding: 0.375rem 0.5rem;
	}
	.mono {
		font-family: var(--font-mono, monospace);
	}
	.dim {
		color: var(--color-muted-foreground);
	}
	.pill {
		border: 1px solid var(--color-border);
		border-radius: 0.25rem;
		padding: 0 0.375rem;
		font-size: 0.6875rem;
	}
	.pill[data-ok='true'] {
		color: var(--color-primary);
	}
	.pill[data-ok='false'] {
		color: var(--color-destructive);
	}
</style>
