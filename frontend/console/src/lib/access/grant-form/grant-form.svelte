<script lang="ts" module>
	import type { ApiResult } from '#lib/api/client.ts';
	import type { AccessGrant } from '#lib/data/namespace.ts';

	/** A grant or a revoke of `relation` to `user`, bound by the page to its object. */
	export type GrantFormMutate = (user: string, relation: string) => Promise<ApiResult<AccessGrant>>;

	export type GrantFormProps = {
		/** The object's type. Names it in every message ("…on this table"), and decides whether the
		 *  steward's entry is offered: the catalog has no `classifier` on a project. */
		kind: string;
		/** The subject being granted to. `$bindable` because AccessGraph prefills it when a subject node
		 *  is clicked on the canvas, which is that component's whole reason for existing. */
		subject?: string;
		/** The caller's own verdicts (`{ can_grant_reader: true, … }`). `null`/absent means UNKNOWN, which
		 *  renders the controls live — this gate exists to explain a refusal, never to invent one from a
		 *  read that has not landed. */
		permissions?: Record<string, boolean> | null;
		/** Subject ids this object already grants to. Used only to suppress the unresolvable advisory for
		 *  a subject that demonstrably resolves here. */
		knownSubjects?: Iterable<string>;
		grant: GrantFormMutate;
		revoke: GrantFormMutate;
		/** Called after a SUCCESSFUL mutation, so a caller can refresh whatever it renders. */
		onmutated?: () => void | Promise<void>;
	};
</script>

<script lang="ts">
	// THE grant/revoke form, the one the grants panel and the access graph both render, so the rungs
	// offered, the gate each button names and the subject advisory are decided once. `grant` and
	// `revoke` arrive as props, bound by the page to its object.
	import { GatedAction } from '../gated-action/index.ts';
	import { Select } from '@riksarkivet/ui/select';
	import { TriangleAlert } from '@lucide/svelte';

	let {
		kind,
		subject = $bindable(''),
		permissions = null,
		knownSubjects = [],
		grant,
		revoke,
		onmutated,
	}: GrantFormProps = $props();

	// What the picker offers, in ONE place, so every surface that grants offers the same entries. An
	// entry names the rungs it grants: one for a rung, two for the steward's pair.
	type Entry = { value: string; name: string; label: string; rungs: readonly string[] };
	const rung = (value: string, label: string = value): Entry => ({
		value,
		name: value,
		label,
		rungs: [value],
	});
	// The access ladder, lowest first: each level holds what the ones before it hold.
	const LADDER: readonly Entry[] = [
		rung('describer', 'describer — may see it, reads no data'),
		rung('reader'),
		rung('writer'),
		rung('modifier', 'modifier — may change and retire it, grants nothing'),
		rung('owner'),
	];
	// `classifier` labels columns and does NOT see the object, so the picker offers it only together
	// with `describer`: a steward is granted both. `describer` is listed first because it is granted
	// first and revoked last, so no failure half-way leaves `classifier` held alone.
	const STEWARD: Entry = {
		value: 'classifier+describer',
		name: 'classifier + describer',
		label: 'classifier + describer — may label it and see it, reads no data',
		rungs: ['describer', 'classifier'],
	};
	// The catalog's model defines `classifier` on these object types and not on a project.
	const CLASSIFIER_KINDS: ReadonlySet<string> = new Set(['warehouse', 'namespace', 'table']);
	const GRANT_AXIS: readonly Entry[] = [
		rung('pass_grants', 'pass_grants — may re-grant what they hold'),
		rung('manage_grants', 'manage_grants — may grant anything, reads nothing'),
	];
	const entries = $derived(
		CLASSIFIER_KINDS.has(kind) ? [...LADDER, STEWARD, ...GRANT_AXIS] : [...LADDER, ...GRANT_AXIS],
	);

	let relation = $state('');
	let busy = $state(false);
	let result = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);
	const selected = $derived(entries.find((e) => e.value === relation));

	// #143 verdicts, per BUTTON, because the gates genuinely differ: grant is `can_grant_<rung>` for
	// EVERY rung the entry grants, revoke is `can_revoke_grant` (= `manage_grants` alone, deliberately
	// stricter so a delegate cannot strip the owner who delegated to them). Conflating the two is the
	// bug the denial message carried.
	const missing = $derived(
		permissions == null || !selected
			? []
			: selected.rungs.filter((r) => permissions[`can_grant_${r}`] !== true),
	);
	const mayGrant = $derived(missing.length === 0);
	const mayRevoke = $derived(permissions == null ? true : permissions.can_revoke_grant === true);

	const grantNeeds = (r: string): string =>
		`Granting ${r || 'a rung'} here needs can_grant_${r || '<rung>'} on this ${kind} — held by a grant-manager, or by someone holding ${r || 'that rung'} plus the grant option. Owning the ${kind} is not sufficient if access is centrally managed.`;
	const gates = (rungs: readonly string[]): string =>
		rungs.map((r) => `can_grant_${r}`).join(' and ');
	const grantReason = $derived.by(() => {
		if (!selected || selected.rungs.length === 1) return grantNeeds(selected?.rungs[0] ?? '');
		const lacking = missing.length
			? `; ${gates(missing)} ${missing.length === 1 ? 'is' : 'are'} missing`
			: '';
		return `Granting ${selected.name} here makes ${selected.rungs.length} grants and needs ${gates(selected.rungs)} on this ${kind}${lacking} — each held by a grant-manager, or by someone holding that rung plus the grant option. Owning the ${kind} is not sufficient if access is centrally managed.`;
	});
	const revokeReason = $derived(
		`Revoking here needs can_revoke_grant on this ${kind} — grant-manager only, deliberately stricter than granting so a delegate cannot strip the owner who delegated to them.`,
	);

	// A grant names a principal, `<idp-id>~<user id>` (LH-063), or a typed object or userset; the catalog refuses
	// a bare name, which no token could ever prove. ADVISORY, so the reason shows before the request, never
	// blocking: the catalog's refusal is the rule.
	const known = $derived(new Set(knownSubjects));
	const looksUnresolvable = $derived.by(() => {
		const u = subject.trim();
		if (!u || u.includes('~') || u.includes(':') || u.includes('#')) return false;
		return !(known.has(u) || known.has(`user:${u}`));
	});

	/** Why the catalog refused one rung's grant or revoke. */
	function refusal(granting: boolean, r: string, res: { status: number; detail: string }): string {
		// NAMES THE REAL GATE. Listing and probing access (`access/list`, `access/check`) ask
		// `can_review_access`, the owner's and the grant manager's; grant and revoke ask the grant
		// axis, which is separate from ownership. Under `managed_access` a refused caller may already
		// HOLD the owner rung, because
		// that flag withdraws precisely `manage_grants` and the grant option beneath it.
		if (res.status === 401 || res.status === 403) return granting ? grantNeeds(r) : revokeReason;
		// The catalog says which half it refused, the subject or the rung.
		if (res.status === 400 || res.status === 422) return res.detail;
		return `Failed (HTTP ${res.status}).`;
	}

	async function run(granting: boolean): Promise<void> {
		const user = subject.trim();
		const entry = selected;
		if (busy || !user || !entry) return;
		busy = true;
		result = null;
		const did = granting ? 'granted to' : 'revoked from';
		// One request per rung, in sequence, stopping at the first refusal. A revoke runs the entry's
		// rungs backwards, so the steward's `classifier` goes before the `describer` it relies on.
		const rungs = granting ? entry.rungs : [...entry.rungs].reverse();
		const done: string[] = [];
		let named = user;
		try {
			for (const r of rungs) {
				const res = granting ? await grant(user, r) : await revoke(user, r);
				if (!res.ok) {
					const why = refusal(granting, r, res);
					// An entry of several rungs names the rung that failed, and any that went through.
					const before = done.length ? `${done.join(', ')} ${did} ${named}; ` : '';
					const text =
						rungs.length === 1
							? why
							: `${before}${r} was not ${granting ? 'granted' : 'revoked'}: ${why}`;
					result = { tone: 'fail', text };
					break;
				}
				done.push(r);
				named = res.data.user;
			}
			if (done.length === rungs.length) {
				result = { tone: 'ok', text: `${entry.name} ${did} ${named}.` };
			}
			if (done.length) await onmutated?.();
		} finally {
			busy = false;
		}
	}
</script>

<div data-slot="grant-form" class="form">
	<input
		class="mono"
		placeholder="principal (idp~user id), or role:…#assignee"
		aria-label="Subject to grant to"
		bind:value={subject}
	/>
	<Select
		bind:value={relation}
		ariaLabel="Grant rung"
		placeholder="rung…"
		options={entries.map((e) => ({ value: e.value, label: e.label }))}
	/>
	<!-- #143: a refused action stays VISIBLE and says why. `disabled` is conditional on the verdict
	     because GatedAction deliberately avoids the native attribute — it would kill the tooltip and
	     drop the control from the tab order — so a natively-disabled child defeats the mechanism.
	     Form-validity disabling still applies in the ALLOWED case, where it is the only thing between
	     the user and a pointless request. -->
	<GatedAction
		allowed={mayGrant}
		action={`Grant ${selected?.name ?? 'a rung'}`}
		reason={grantReason}
	>
		<button
			class="btn"
			disabled={mayGrant && (busy || !subject.trim() || !selected)}
			onclick={() => run(true)}
		>
			{busy ? '…' : 'Grant'}
		</button>
	</GatedAction>
	<GatedAction allowed={mayRevoke} action="Revoke" reason={revokeReason}>
		<button
			class="btn ghost"
			disabled={mayRevoke && (busy || !subject.trim() || !selected)}
			onclick={() => run(false)}
		>
			Revoke
		</button>
	</GatedAction>
	{#if looksUnresolvable}
		<p class="advice">
			<TriangleAlert size={12} />
			<span>
				Not a principal. A signed-in person is <code class="mono"
					>&lt;idp-id&gt;~&lt;user id&gt;</code
				>
				— the identity provider that vouched for them and the id it reports, like
				<code class="mono">dex~08a8684b…</code>, not a display name — so the catalog refuses
				<code class="mono">{subject.trim()}</code>.
			</span>
		</p>
	{/if}
	{#if result}
		<p class={['verdict', { ok: result.tone === 'ok', fail: result.tone === 'fail' }]}>
			{result.text}
		</p>
	{/if}
</div>

<style>
	.form {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}
	.mono {
		flex: 1 1 220px;
		min-width: 0;
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 12px;
		padding: 4px 8px;
		border: 1px solid var(--color-border);
		border-radius: 6px;
		background: var(--color-background);
		color: var(--color-foreground);
	}
	.btn {
		font-size: 12px;
		padding: 4px 10px;
		border: 1px solid var(--color-border);
		border-radius: 6px;
		background: var(--color-card);
		color: var(--color-foreground);
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.btn.ghost {
		background: transparent;
	}
	.advice,
	.verdict {
		flex-basis: 100%;
		display: flex;
		align-items: flex-start;
		gap: 6px;
		margin: 0;
		font-size: 11px;
		line-height: 1.5;
		color: var(--color-muted-foreground);
	}
	.verdict.ok {
		color: var(--color-success, var(--color-muted-foreground));
	}
	.verdict.fail {
		color: var(--color-destructive);
	}
	code {
		font-size: 10px;
	}
</style>
