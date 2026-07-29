<script lang="ts">
	// The FGA workbench's Check tab — a live OpenFGA Check on any (user, relation, object) triple via
	// POST /v1/access/check. The verdict renders BIG, alongside the exact triple the catalog echoes
	// back (`checked`) so what was evaluated is never inferred from local form state.
	import { CircleCheck, CircleX, ShieldAlert } from '@lucide/svelte';
	import { parse } from '@rask/api';
	import { page } from '$app/state';
	import { checkAccess, CheckVerdictSchema, type CheckVerdict } from './access';

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let user = $state('');
	let relation = $state('');
	let object = $state('');
	let busy = $state(false);
	let verdict = $state<CheckVerdict | null>(null);
	let failure = $state<{ status: number; text: string } | null>(null);

	async function run(): Promise<void> {
		const triple = { user: user.trim(), relation: relation.trim(), object: object.trim() };
		if (busy || !triple.user || !triple.relation || !triple.object) return;
		busy = true;
		verdict = null;
		failure = null;
		try {
			const res = await checkAccess(triple);
			if (res.ok) {
				try {
					verdict = parse(CheckVerdictSchema, res.data);
				} catch (err) {
					console.error(`access check parse failure: ${String(err)}`);
					failure = { status: -1, text: 'The check payload drifted from the contract.' };
				}
			} else if (res.status === 401) {
				failure = { status: 401, text: 'Sign in to run a check.' };
			} else if (res.status === 403) {
				failure = {
					status: 403,
					text: 'Checks are estate-admin only (probing the graph == disclosing it).',
				};
			} else {
				failure = { status: res.status, text: res.detail };
			}
		} finally {
			busy = false;
		}
	}
</script>

<div class="check">
	<p class="lead">
		Does <span class="mono">user</span> hold <span class="mono">relation</span> on
		<span class="mono">object</span>? One live Check against the running OpenFGA store — direct tuples
		and everything the model derives.
	</p>
	<form
		class="form"
		onsubmit={(e) => {
	e.preventDefault();
	run();
}}
	>
		<input
			class="mono"
			bind:value={user}
			placeholder="user (e.g. user:alice)"
			aria-label="Check user"
		/>
		<input
			class="mono"
			bind:value={relation}
			placeholder="relation (e.g. can_read_data)"
			aria-label="Check relation"
		/>
		<input
			class="mono"
			bind:value={object}
			placeholder="object (e.g. table:db1$t)"
			aria-label="Check object"
		/>
		<button
			class="btn"
			type="submit"
			disabled={busy || !user.trim() || !relation.trim() || !object.trim()}
		>
			{busy ? '…' : 'Check'}
		</button>
	</form>

	{#if failure}
		<div class="fail-note" role="alert">
			<ShieldAlert size={15} />
			{failure.text}
			{#if failure.status === 401}<a href={loginHref} data-sveltekit-reload>Sign in</a>{/if}
		</div>
	{:else if verdict}
		<div class="verdict" class:allowed={verdict.allowed} class:denied={!verdict.allowed}>
			{#if verdict.allowed}<CircleCheck size={30} />{:else}<CircleX size={30} />{/if}
			<span class="word">{verdict.allowed ? 'ALLOWED' : 'DENIED'}</span>
		</div>
		<p class="triple mono" aria-label="Checked triple">
			({verdict.checked.user}, {verdict.checked.relation}, {verdict.checked.object})
		</p>
	{/if}
</div>

<style>
	.check {
		display: flex;
		flex-direction: column;
		gap: 14px;
		max-width: 640px;
	}
	.lead {
		color: var(--mut);
		font-size: 12px;
		margin: 0;
	}
	.form {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	.form input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 8px;
		flex: 1 1 170px;
		min-width: 140px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 16px;
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.verdict {
		display: flex;
		align-items: center;
		gap: 12px;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 18px 22px;
	}
	.verdict .word {
		font-size: 28px;
		font-weight: 700;
		letter-spacing: 0.04em;
	}
	.verdict.allowed {
		color: var(--ok);
		border-color: color-mix(in srgb, var(--ok) 50%, var(--line));
		background: color-mix(in srgb, var(--ok) 7%, transparent);
	}
	.verdict.denied {
		color: var(--fail);
		border-color: color-mix(in srgb, var(--fail) 50%, var(--line));
		background: color-mix(in srgb, var(--fail) 7%, transparent);
	}
	.triple {
		color: var(--mut);
		font-size: 13px;
		margin: 0;
	}
	.fail-note {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--fail);
		font-size: 13px;
	}
	.fail-note a {
		color: var(--accent);
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
