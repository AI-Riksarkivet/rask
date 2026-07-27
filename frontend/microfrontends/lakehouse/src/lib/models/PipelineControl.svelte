<script lang="ts">
	// `/pipeline` — the #64 human trigger door. Fire the medallion cascade head (/produce) or a training
	// run (/train) from the UI. Both are admin-gated by lance-ray (can_administer on the project); the BFF
	// forwards ONLY the signed-in user's bearer, so an anonymous visitor sees the sign-in banner and a
	// non-admin the denial banner — never a silent no-op. The service-to-service app-token path is unchanged.
	import { GitBranch, Play, ShieldAlert } from '@lucide/svelte';
	import { triggerProduce, triggerTrain } from './medallion';

	let busy = $state(false);
	let banner = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);
	let train = $state({ model: '', features: '' });

	function fail(status: number, detail: string): void {
		if (status === 401)
			banner = {
				tone: 'fail',
				text: 'Sign in — triggering the pipeline is a per-user admin action.',
			};
		else if (status === 403)
			banner = {
				tone: 'fail',
				text: 'Denied: firing the pipeline needs the project-admin rung (can_administer).',
			};
		else if (status === 409)
			banner = { tone: 'fail', text: 'Train head not configured on this stack (needs Ray + S3).' };
		else banner = { tone: 'fail', text: detail };
	}

	async function produce(): Promise<void> {
		if (busy) return;
		busy = true;
		banner = null;
		try {
			const res = await triggerProduce();
			if (res.ok)
				banner = { tone: 'ok', text: `Cascade fired — run token ${res.data.token ?? '(none)'}.` };
			else fail(res.status, res.detail);
		} finally {
			busy = false;
		}
	}

	async function submitTrain(): Promise<void> {
		if (busy || !train.model.trim() || !train.features.trim()) return;
		busy = true;
		banner = null;
		try {
			// Comma- or newline-separated `stage$name` feature datasets → pointer refs (claim-check).
			const features = train.features
				.split(/[\n,]/)
				.map((s) => s.trim())
				.filter(Boolean)
				.map((dataset) => ({ dataset }));
			const res = await triggerTrain(train.model.trim(), features);
			if (res.ok) {
				banner = {
					tone: 'ok',
					text: `Training requested for ${res.data.model ?? train.model} — token ${res.data.token ?? '(none)'}.`,
				};
				train = { model: '', features: '' };
			} else fail(res.status, res.detail);
		} finally {
			busy = false;
		}
	}
</script>

<div class="page">
	<header>
		<GitBranch size={16} />
		<h1>Pipeline</h1>
		<span class="sub mono">event-driven cascade + train head · project-admin gated</span>
	</header>

	{#if banner}
		<div class="banner" class:ok={banner.tone === 'ok'} class:fail={banner.tone === 'fail'}>
			{banner.text}
		</div>
	{/if}

	<section>
		<h2>Produce</h2>
		<p class="mut">
			Fire the cascade head — seeds <span class="mono">raw_events</span> and emits its arrival event,
			which drives raw → bronze → silver → gold. The run's lineage appears on the
			<a href="/lakehouse/lineage" data-sveltekit-reload>Lineage</a> view.
		</p>
		<button class="btn primary" disabled={busy} onclick={produce}>
			<Play size={13} /> Run cascade
		</button>
	</section>

	<section>
		<h2>Train</h2>
		<p class="mut">
			Request a training run — pointers only: a target model and one or more
			<span class="mono">stage$name</span> feature datasets (comma or newline separated). Omitted versions
			resolve to latest.
		</p>
		<form
			class="col"
			onsubmit={(e) => {
	e.preventDefault();
	submitTrain();
}}
		>
			<input
				class="mono"
				bind:value={train.model}
				placeholder="model name (e.g. churn)"
				aria-label="Model name"
			/>
			<textarea
				class="mono"
				bind:value={train.features}
				rows="2"
				placeholder="silver$features_daily, silver$features_hourly"
				aria-label="Feature datasets"></textarea>
			<button
				class="btn"
				type="submit"
				disabled={busy || !train.model.trim() || !train.features.trim()}
			>
				Request training
			</button>
		</form>
	</section>

	<section class="note">
		<ShieldAlert size={14} />
		<p class="mut">
			Both triggers are guarded by OpenFGA — only a signed-in user who holds
			<span class="mono">can_administer</span> on the project may fire them. Service-to-service callers use
			the shared app-token; the web tier never holds it.
		</p>
	</section>
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
		gap: 10px;
		margin-bottom: 18px;
	}
	h1 {
		font-size: 20px;
		margin: 0;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 18px 0 8px;
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
	.col {
		display: flex;
		flex-direction: column;
		gap: 8px;
		max-width: 520px;
	}
	input,
	textarea {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 5px 9px;
		font-size: 12px;
		resize: vertical;
	}
	.btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		align-self: flex-start;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 5px 12px;
		cursor: pointer;
	}
	.btn.primary {
		border-color: color-mix(in srgb, var(--amber, #ffc14d) 60%, var(--line));
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
		margin: 0 0 8px;
	}
	.note {
		display: flex;
		gap: 8px;
		align-items: flex-start;
		margin-top: 24px;
		padding-top: 16px;
		border-top: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
	}
	.note .mut {
		margin: 0;
	}
</style>
