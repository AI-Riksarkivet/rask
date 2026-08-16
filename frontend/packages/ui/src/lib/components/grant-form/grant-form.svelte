<script lang="ts" module>
	/** Matches the zones' status-aware `ApiResult`. Declared STRUCTURALLY rather than imported, on the
	 *  GrantsPanel precedent: this library owns no API client, so a zone hands its own remotes in. */
	export type GrantFormResult =
		| { ok: true; data: { user: string } }
		| { ok: false; status: number; detail: string };

	export type GrantFormMutate = (user: string, relation: string) => Promise<GrantFormResult>;

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
	// THE grant/revoke form. One implementation, and that is the point of the file.
	//
	// It existed twice — here in `@rask/ui`'s GrantsPanel and again in the lakehouse's AccessGraph —
	// and on 2026-08-16 three separate defects were found and fixed IN BOTH COPIES on the same day:
	// a denial message naming the wrong gate, buttons that ignored permission entirely (#143), and a
	// form that invited a subject id which can never match a signed-in user. Each was one bug that had
	// to be paid for twice, and the copies had already drifted where nobody was looking — AccessGraph
	// offered four rungs while this offered six, so `pass_grants` and `manage_grants` were ungrantable
	// from one surface and grantable from the other with no decision behind the difference.
	//
	// Transport-agnostic like every component here: `grant`/`revoke` arrive as async props and the
	// library never imports a zone remote.
	import { GatedAction } from '../gated-action/index.js';
	import { Select } from '../select/index.js';
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

	// The rung ladder, in ONE place. The two copies disagreed: four rungs against six.
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

	// A tuple is written for the id EXACTLY as typed while a live store keys on the OIDC `sub`, so a
	// display name grants to nobody AND still answers 200. ADVISORY, never blocking: literal ids are
	// legitimate (a service account holds rungs on this estate) and usersets are literal by design.
	const known = $derived(new Set(knownSubjects));
	const looksUnresolvable = $derived.by(() => {
		const u = subject.trim();
		if (!u || u.includes(':') || u.includes('#')) return false;
		if (known.has(u) || known.has(`user:${u}`)) return false;
		return /^[a-z][a-z0-9._-]{0,30}$/i.test(u);
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
				result = { tone: 'fail', text: `${relation} is not a grantable rung here.` };
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
		placeholder="OIDC sub, or role:…#assignee / team:…#member"
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
				Granted exactly as typed. A signed-in user's subject is their OIDC <code>sub</code> — a long
				opaque id like <code class="mono">CiQwOGE4Njg0Yi…</code>, not a display name — so
				<code class="mono">{subject.trim()}</code> will match nobody unless that is literally the
				subject id (a service account or a userset).
			</span>
		</p>
	{/if}
	{#if result}
		<p class="verdict" class:ok={result.tone === 'ok'} class:fail={result.tone === 'fail'}>
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
