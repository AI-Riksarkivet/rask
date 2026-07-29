<script lang="ts">
	// `/models` — the model-registry view (#42): every registered model with its candidate (latest)
	// and blessed versions, a per-model metrics comparison, and the candidate→blessed promote action.
	// Data comes through the /capi BFF (catalog is OIDC-only — see routes/capi/[...path]/+server.ts):
	// signed-out on a governed stack ⇒ 401 ⇒ the sign-in state below, never a broken table.
	import { Chip } from '@rask/ui/chip';
	import { Award, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { page } from '$app/state';
	import {
		fetchModel,
		fetchModels,
		type ModelDescribe,
		type ModelSummary,
		promoteModel,
	} from './catalog';
	import ModelDetail from './ModelDetail.svelte';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let models = $state<ModelSummary[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false); // distinguishes "still loading" (0, unsettled) from a network error (0, settled)
	let selected = $state<string | null>(null);
	let detail = $state<ModelDescribe | null>(null);
	let promoting = $state(false);
	let banner = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);
	let polling = false;

	// 401 before ANY successful load = governed stack without a session; a poll-tick blip after a
	// successful load keeps the previous list (the store convention from LineageExplorer, audit B1).
	// 0 stays IN the offline set once settled: a fetch-level failure (network down, BFF timeout) must
	// render as offline, not hang on the loading message across poll ticks (audit finding).
	const unauthorized = $derived(models === null && lastStatus === 401);
	const offline = $derived(models === null && settled && lastStatus !== 401);

	async function refresh(): Promise<void> {
		if (polling) return;
		polling = true;
		try {
			const res = await fetchModels();
			settled = true;
			if (res.ok) {
				models = res.data.models;
				lastStatus = 200;
			} else {
				lastStatus = res.status;
			}
			const current = selected;
			if (current) {
				const one = await fetchModel(current);
				// Latest-wins: drop the response if the user clicked away while it was in flight.
				if (one.ok && selected === current) detail = one.data;
			}
		} finally {
			polling = false;
		}
	}

	// A model version is written by a training run, and a run is lineage — so a new candidate, a fresh
	// metric or a promotion all move this cursor. Registry-wide and for the open detail alike (`refresh`
	// re-reads the selected model too), so the list and the drill-in can never show different versions.
	liveRead(lineageTick, () => refresh());

	async function select(model: string): Promise<void> {
		banner = null;
		if (selected === model) {
			selected = null;
			detail = null;
			return;
		}
		selected = model;
		detail = null;
		const res = await fetchModel(model);
		// Latest-wins: ignore a response for a model the user has already clicked away from.
		if (selected === model && res.ok) detail = res.data;
	}

	async function promote(model: string, version: number): Promise<void> {
		promoting = true;
		banner = null;
		try {
			const res = await promoteModel(model, version);
			if (res.ok) {
				banner = { tone: 'ok', text: `${model} v${res.data.blessed_version} is now blessed` };
			} else if (res.status === 401) {
				banner = { tone: 'fail', text: 'Sign in to promote — promotion is a per-user action.' };
			} else if (res.status === 403) {
				banner = {
					tone: 'fail',
					text: 'Denied: promotion needs the validator rung (can_promote).',
				};
			} else {
				banner = { tone: 'fail', text: res.detail };
			}
			await Promise.all([
				fetchModels().then((r) => {
					if (r.ok) models = r.data.models;
				}),
				fetchModel(model).then((r) => {
					if (r.ok && selected === model) detail = r.data;
				}),
			]);
		} finally {
			promoting = false;
		}
	}
</script>

<div class="page">
	<header>
		<h1>Model registry</h1>
		<span class="sub mono">candidate → blessed · models$&lt;model&gt;</span>
	</header>

	{#if banner}
		<div class="banner" class:ok={banner.tone === 'ok'} class:fail={banner.tone === 'fail'}>
			{banner.text}
		</div>
	{/if}

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to view the model registry.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}) — retrying.</p>
		</div>
	{:else if models === null}
		<div class="empty"><p>Loading…</p></div>
	{:else if models.length === 0}
		<div class="empty">
			<p>No models registered yet — a training run publishes the first registry version.</p>
		</div>
	{:else}
		<table>
			<thead>
				<tr><th>model</th><th>candidate</th><th>blessed</th><th>state</th><th></th></tr>
			</thead>
			<tbody>
				{#each models as m (m.model)}
					{@const latest = m.latest_version ?? null}
					{@const blessed = m.blessed_version ?? null}
					<tr class:active={selected === m.model} onclick={() => select(m.model)}>
						<td class="mono name">{m.model}</td>
						<td class="mono">{latest === null ? '—' : `v${latest}`}</td>
						<td class="mono">{blessed === null ? '—' : `v${blessed}`}</td>
						<td>
							{#if blessed !== null && blessed === latest}
								<Chip label="blessed" tone="accent" />
							{:else if blessed !== null}
								<Chip label="blessed behind" />
							{:else if latest !== null}
								<Chip label="candidate only" />
							{:else}
								<Chip label="unreadable" />
							{/if}
						</td>
						<td class="actions">
							{#if latest !== null && latest !== blessed}
								<button
									class="promote"
									disabled={promoting}
									onclick={(e) => {
	e.stopPropagation();
	promote(m.model, latest);
}}
								>
									<Award size={13} /> bless v{latest}
								</button>
							{/if}
						</td>
					</tr>
					{#if selected === m.model}
						<tr class="detail-row">
							<td colspan="5">
								{#if detail === null}
									<p class="mut">Loading model detail…</p>
								{:else}
									<!-- Goal cond 9: metrics comparison + artifacts table + training curves. -->
									<ModelDetail model={m.model} {detail} />
								{/if}
							</td>
						</tr>
					{/if}
				{/each}
			</tbody>
		</table>
	{/if}
</div>

<style>
	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 12px;
		margin-bottom: 18px;
	}
	h1 {
		font-size: 20px;
		margin: 0;
	}
	.sub {
		color: var(--faint);
		font-size: 12px;
	}
	.banner {
		padding: 8px 12px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--line);
		margin-bottom: 12px;
		font-size: 13px;
	}
	.banner.ok {
		border-color: color-mix(in srgb, var(--ok) 45%, var(--line));
		color: var(--ok);
	}
	.banner.fail {
		border-color: color-mix(in srgb, var(--fail) 45%, var(--line));
		color: var(--fail);
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		border: 1px dashed var(--line);
		border-radius: var(--radius);
		padding: 22px;
	}
	.empty a {
		color: var(--accent);
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 13px;
	}
	th {
		text-align: left;
		color: var(--faint);
		font-weight: 500;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		padding: 6px 10px;
		border-bottom: 1px solid var(--line);
	}
	td {
		padding: 9px 10px;
		border-bottom: 1px solid var(--line);
	}
	tbody tr:not(.detail-row) {
		cursor: pointer;
	}
	tbody tr:not(.detail-row):hover,
	tbody tr.active {
		background: var(--panel);
	}
	.name {
		color: var(--ink);
		font-weight: 600;
	}
	.actions {
		text-align: right;
	}
	.promote {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 4px 10px;
		border: 1px solid color-mix(in srgb, var(--amber) 55%, var(--line));
		border-radius: 7px;
		background: var(--panel-2);
		color: var(--amber);
		font: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.promote:hover:not(:disabled) {
		background: var(--panel);
	}
	.promote:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.detail-row td {
		background: var(--bg-2);
		padding: 12px 16px;
	}
	.mut {
		color: var(--mut);
		margin: 0;
	}
</style>
