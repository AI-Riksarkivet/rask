<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/** `<rask-compute-serve>` — Ray Serve applications, served by the compute zone to the global
	 *  workbench. */
	import { serveApplications, type ServePayload } from '@rask/api';
	import { RayPoll } from './ray-poll.svelte';

	let { pollms = 5000 }: { pollms?: number } = $props();
	const poll = new RayPoll<ServePayload>((f) => serveApplications(f));
	$effect(() => poll.start(pollms));

	const apps = $derived(Object.values(poll.data?.applications ?? {}));
</script>

<div class="ce-panel">
	<p class="meta">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if poll.error !== null}
		<p class="error">Serve unreachable: {poll.error}</p>
	{:else if apps.length === 0}
		<p class="empty">No Serve applications deployed.</p>
	{:else}
		<table>
			<thead>
				<tr><th>Application</th><th>Route</th><th>Status</th><th>Deployments</th></tr>
			</thead>
			<tbody>
				{#each apps as app (app.name)}
					<tr>
						<td class="mono">{app.name}</td>
						<td class="mono">{app.route_prefix ?? '—'}</td>
						<td><span class="pill" data-ok={app.status === 'RUNNING'}>{app.status}</span></td>
						<td>{Object.keys(app.deployments).length}</td>
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
	.pill {
		border: 1px solid var(--color-border);
		border-radius: 0.25rem;
		padding: 0 0.375rem;
		font-size: 0.6875rem;
	}
	.pill[data-ok='true'] {
		color: var(--color-primary);
	}
</style>
