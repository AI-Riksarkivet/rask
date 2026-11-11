<script lang="ts" module>
	import type { ApiResult } from '#lib/api/client.ts';
	import type { AccessGrant } from '#lib/data/namespace.ts';

	/** A grant or a revoke of `relation` to `user`, bound by the page to its object. */
	export type GrantFormMutate = (user: string, relation: string) => Promise<ApiResult<AccessGrant>>;

	export type GrantFormProps = {
		/** Names the object in every message — "…on this table" / "…on this namespace". */
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
	import { Select } from '@rask/ui/select';
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

	// The rung ladder, in ONE place, so every surface that grants offers the same rungs.
	const GRANTABLE = ['reader', 'writer', 'validator', 'owner', 'pass_grants', 'manage_grants'];
	const RUNG_LABEL: Record<string, string> = {
		pass_grants: 'pass_grants — may re-grant what they hold',
		manage_grants: 'manage_grants — may grant anything, reads nothing',
	};

	let relation = $state('');
	let busy = $state(false);
	let result = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);

	// #143 verdicts, per BUTTON, because the gates genuinely differ: grant is `can_grant_<rung>`,
	// revoke is `can_revoke_grant` (= `manage_grants` alone, deliberately stricter so a delegate cannot
	// strip the owner who delegated to them). Conflating the two is the bug the denial message carried.
	const mayGrant = $derived(
		permissions == null || !relation ? true : permissions[`can_grant_${relation}`] === true,
	);
	const mayRevoke = $derived(permissions == null ? true : permissions.can_revoke_grant === true);

	const grantReason = $derived(
		`Granting ${relation || 'a rung'} here needs can_grant_${relation || '<rung>'} on this ${kind} — held by a grant-manager, or by someone holding ${relation || 'that rung'} plus the grant option. Owning the ${kind} is not sufficient if access is centrally managed.`,
	);
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

	async function run(granting: boolean): Promise<void> {
		const user = subject.trim();
		if (busy || !user || !relation) return;
		busy = true;
		result = null;
		try {
			const res = granting ? await grant(user, relation) : await revoke(user, relation);
			if (res.ok) {
				result = {
					tone: 'ok',
					text: `${relation} ${granting ? 'granted to' : 'revoked from'} ${res.data.user}.`,
				};
				await onmutated?.();
			} else if (res.status === 401 || res.status === 403) {
				// NAMES THE REAL GATE. It read "Managing access needs the owner tier", which is true of
				// `access/list` and `access/check` (both still `can_drop`) and has NOT been true of
				// grant/revoke since the grant axis was separated from ownership. Under `managed_access` a
				// refused caller may already HOLD the owner tier, because that flag withdraws precisely
				// `manage_grants` and the grant option beneath it.
				result = {
					tone: 'fail',
					text: granting ? grantReason : revokeReason,
				};
			} else if (res.status === 400 || res.status === 422) {
				// The catalog says which half it refused, the subject or the rung.
				result = { tone: 'fail', text: res.detail };
			} else {
				result = { tone: 'fail', text: `Failed (HTTP ${res.status}).` };
			}
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
		options={GRANTABLE.map((r) => ({ value: r, label: RUNG_LABEL[r] ?? r }))}
	/>
	<!-- #143: a refused action stays VISIBLE and says why. `disabled` is conditional on the verdict
	     because GatedAction deliberately avoids the native attribute — it would kill the tooltip and
	     drop the control from the tab order — so a natively-disabled child defeats the mechanism.
	     Form-validity disabling still applies in the ALLOWED case, where it is the only thing between
	     the user and a pointless request. -->
	<GatedAction allowed={mayGrant} action={`Grant ${relation || 'a rung'}`} reason={grantReason}>
		<button
			class="btn"
			disabled={mayGrant && (busy || !subject.trim() || !relation)}
			onclick={() => run(true)}
		>
			{busy ? '…' : 'Grant'}
		</button>
	</GatedAction>
	<GatedAction allowed={mayRevoke} action="Revoke" reason={revokeReason}>
		<button
			class="btn ghost"
			disabled={mayRevoke && (busy || !subject.trim() || !relation)}
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
