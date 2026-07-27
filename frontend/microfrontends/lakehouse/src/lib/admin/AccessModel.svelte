<script lang="ts">
	// The FGA workbench's Model tab — the checked-in model.fga DSL, read-only by design: the
	// authorization model is code (3 synced model files, migrated through the repo), so the UI shows
	// it verbatim with its live authorization_model_id and offers no edit affordance whatsoever.
	import { FileCode2, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { parse } from '@rask/api';
	import { page } from '$app/state';
	import { AccessModelSchema, fetchAccessModel, type AccessModel } from './access';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let model = $state<AccessModel | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let inflight = 0;

	const unauthorized = $derived(model === null && settled && lastStatus === 401);
	const forbidden = $derived(model === null && settled && lastStatus === 403);
	const drifted = $derived(model === null && settled && lastStatus === -1);
	const offline = $derived(model === null && settled && ![-1, 200, 401, 403].includes(lastStatus));

	async function load(): Promise<void> {
		const seq = ++inflight;
		const res = await fetchAccessModel();
		if (seq !== inflight) return; // latest-wins
		settled = true;
		if (res.ok) {
			try {
				model = parse(AccessModelSchema, res.data);
				lastStatus = 200;
			} catch (err) {
				console.error(`access model parse failure: ${String(err)}`);
				model = null;
				lastStatus = -1;
			}
		} else {
			model = null;
			lastStatus = res.status;
		}
	}

	$effect(() => {
		load();
	});
</script>

<div class="model">
	<div class="note" role="note">
		<FileCode2 size={15} />
		<span>
			<strong>Model changes are code migrations.</strong> This view is read-only — the model lives in the
			repo (model.fga, kept in sync with its JSON twins) and changes ship through review + deploy, never
			through a UI write. Grant within the model on the Tuples tab.
		</span>
	</div>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={15} /> <a href={loginHref} data-sveltekit-reload>Sign in</a> to view the model.
		</div>
	{:else if forbidden}
		<div class="empty">
			<ShieldAlert size={15} /> The model view is estate-admin only.
		</div>
	{:else if drifted}
		<div class="empty">
			<ShieldAlert size={15} /> The model payload drifted from the contract — refusing to render it.
		</div>
	{:else if offline}
		<div class="empty"><RefreshCw size={15} /> Catalog unreachable (HTTP {lastStatus}).</div>
	{:else if model === null}
		<div class="empty">Loading the authorization model…</div>
	{:else}
		<p class="id mono">authorization_model_id: {model.authorization_model_id}</p>
		<pre class="dsl" aria-label="Authorization model DSL"><code>{model.dsl}</code></pre>
	{/if}
</div>

<style>
	.model {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.note {
		display: flex;
		align-items: baseline;
		gap: 8px;
		border: 1px solid var(--line);
		background: var(--panel-2);
		border-radius: var(--radius-sm);
		color: var(--mut);
		font-size: 12px;
		padding: 10px 12px;
	}
	.note strong {
		color: var(--ink);
	}
	.id {
		color: var(--faint);
		font-size: 11px;
		margin: 0;
	}
	.dsl {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		line-height: 1.55;
		padding: 14px 16px;
		margin: 0;
		overflow-x: auto;
		font-family: ui-monospace, monospace;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		font-size: 13px;
		padding: 24px 0;
	}
	.empty a {
		color: var(--accent);
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
