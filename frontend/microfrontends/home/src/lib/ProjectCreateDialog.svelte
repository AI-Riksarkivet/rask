<script lang="ts">
	// Project creation — an estate-admin flow that COMPOSES existing APIs: creating the first (work)
	// warehouse under a new project name creates the project implicitly; an optional second create with
	// serving:"gold" provisions the per-tenant gold serving warehouse (DECISIONS "Medallion tiers"); the
	// initial admin grant is one raw FGA tuple written through the shared `writeTuple` remote command
	// (estate-admin gated at the catalog, like /v1/events). Every step toasts success/failure honestly —
	// a partial outcome (work warehouse up, gold or grant failed) is NAMED, never rolled into a fake
	// success.
	//
	// The first create is what MINTS the tenant, and the catalog seeds the caller as the new project's
	// `admin` on that very call (endpoints/warehouses.py — a brand-new project has no tuples, so the
	// estate-admin door opens once and then hands the project to its creator). So the gold create and
	// every later project-scoped op run on the creator's OWN project rung, and the admin field below is
	// for granting a SECOND admin (or handing the tenant to someone else) — never the only thing
	// standing between the new project and being ungovernable.
	//
	// Moved here from the lakehouse zone unchanged in behaviour (2026-08-03 ruling): a project is the
	// TOP of the hierarchy, so the surface that MINTS one belongs beside the estate's project list in
	// the main menu, not inside one project's catalog.
	import { Dialog } from '@rask/ui/dialog';
	import { toast } from 'svelte-sonner';
	import { createWarehouse } from '$lib/remote/warehouses.remote';
	// The grant goes through the SAME remote command the FGA workbench writes with — one write door,
	// so every raw tuple write is validated, audited, control-event-emitting and single-flight alike.
	import { writeTuple } from '$lib/remote/access.remote';

	let {
		open = $bindable(false),
		defaultAdmin = '',
		oncreated,
	}: {
		open?: boolean;
		/** Prefill for the initial admin grant — the signed-in caller in FGA subject form. */
		defaultAdmin?: string;
		oncreated?: () => void;
	} = $props();

	let name = $state('');
	let whId = $state('');
	let whBucket = $state('');
	let goldId = $state('');
	let goldBucket = $state('');
	let admin = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);

	// Prefill the admin subject on the closed→open TRANSITION only (kept editable — the estate admin
	// may be provisioning for someone else, and clearing the field must stick: an unconditional
	// refill would make an empty admin, i.e. "no initial grant", impossible to express).
	let wasOpen = false;
	$effect(() => {
		if (open && !wasOpen) admin = defaultAdmin;
		wasOpen = open;
	});

	function reset(): void {
		name = '';
		whId = '';
		whBucket = '';
		goldId = '';
		goldBucket = '';
		admin = '';
		error = null;
	}

	function failText(what: string, status: number, detail: string): string {
		if (status === 401) return `Sign in — ${what} is a per-user action.`;
		if (status === 403) return `Denied: ${what} needs the estate/project-admin rung.`;
		if (status === 409) return `Conflict: ${what} — the id already exists.`;
		if (status === 0) {
			// Status 0 conflates two different failures (an unreachable catalog is status 0 by the §1.0
			// transport rule): the fetch's own timeout — where the catalog may still commit the write
			// after we gave up — vs a true network refusal where nothing reached it. Only the latter can
			// honestly claim "not applied".
			return /TimeoutError|AbortError/.test(detail)
				? `Timed out waiting on the catalog — ${what} may or may not have been applied; check the projects list before retrying.`
				: `Catalog unreachable — ${what} was not applied.`;
		}
		return detail;
	}

	async function create(): Promise<void> {
		const project = name.trim();
		const wh = whId.trim();
		if (busy || !project || !wh) return;
		busy = true;
		error = null;
		try {
			// 1. The work warehouse — its `project` field is what brings the project into existence.
			const first = await createWarehouse({ id: wh, project, bucket: whBucket.trim() || null });
			if (!first.ok) {
				// No confirmed creation (a timeout may still land server-side — failText says so) — stay
				// open so the form can be corrected and retried.
				error = failText(`provisioning warehouse ${wh}`, first.status, first.detail);
				toast.error(error);
				return;
			}
			const partials: string[] = [];
			// 2. Optional gold serving warehouse (a second, serving-classed record).
			const gold = goldId.trim();
			if (gold) {
				const res = await createWarehouse({
					id: gold,
					project,
					bucket: goldBucket.trim() || null,
					serving: 'gold',
				});
				if (!res.ok) {
					partials.push(
						`gold serving warehouse ${gold} failed: ${failText(`provisioning ${gold}`, res.status, res.detail)}`,
					);
				}
			}
			// 3. Optional extra admin — a raw admin tuple on the new project object, ON TOP of the one the
			// catalog already seeded for the caller during step 1. Left empty, the project is still
			// governable (by its creator); filled, it hands a co-admin (or a successor) the same rung.
			const adminUser = admin.trim();
			if (adminUser) {
				const res = await writeTuple({
					user: adminUser,
					relation: 'admin',
					object: `project:${project}`,
				});
				if (!res.ok) {
					partials.push(
						`admin grant for ${adminUser} failed: ${failText('the admin grant', res.status, res.detail)}`,
					);
				}
			}
			if (partials.length > 0) {
				// The project + work warehouse EXIST — say so, and name exactly what still needs doing.
				toast.error(`Project ${project} created with ${wh}, but ${partials.join('; ')}`);
			} else {
				toast.success(
					`Project ${project} created — warehouse ${wh}${gold ? `, gold serving ${gold}` : ''}${adminUser ? `, admin ${adminUser}` : ''}.`,
				);
			}
			open = false;
			reset();
			oncreated?.();
		} finally {
			busy = false;
		}
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content>
		<Dialog.Title>New project</Dialog.Title>
		<Dialog.Description>
			Provisioning the first warehouse under a new project name creates the project and makes you its
			admin; the optional serving warehouse hosts the gold tier in its own bucket.
		</Dialog.Description>
		<form
			class="form"
			onsubmit={(e) => {
	e.preventDefault();
	create();
}}
		>
			<label
				>project name
				<input class="mono" bind:value={name} placeholder="acme" aria-label="Project name" />
			</label>
			<div class="pair">
				<label
					>first warehouse id
					<input class="mono" bind:value={whId} placeholder="acme-wh" aria-label="Warehouse id" />
				</label>
				<label
					>bucket (defaults to id)
					<input class="mono" bind:value={whBucket} placeholder="—" aria-label="Warehouse bucket" />
				</label>
			</div>
			<div class="pair">
				<label
					>gold serving warehouse (optional)
					<input
						class="mono"
						bind:value={goldId}
						placeholder="acme-gold"
						aria-label="Gold serving warehouse id"
					/>
				</label>
				<label
					>serving bucket (defaults to id)
					<input
						class="mono"
						bind:value={goldBucket}
						placeholder="—"
						aria-label="Gold serving bucket"
					/>
				</label>
			</div>
			<label
				>initial admin (FGA subject)
				<input
					class="mono"
					bind:value={admin}
					placeholder="user:alice"
					aria-label="Initial admin"
				/>
			</label>
			{#if error}<p class="error">{error}</p>{/if}
			<div class="actions">
				<button type="button" class="btn ghost" disabled={busy} onclick={() => (open = false)}>
					Cancel
				</button>
				<button class="btn" type="submit" disabled={busy || !name.trim() || !whId.trim()}>
					{busy ? '…' : 'Create project'}
				</button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>

<style>
	.form {
		display: flex;
		flex-direction: column;
		gap: 10px;
		font-size: 12px;
		color: var(--mut);
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 3px;
		flex: 1;
	}
	.pair {
		display: flex;
		gap: 10px;
	}
	input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 5px 9px;
		font-size: 12px;
	}
	.error {
		color: var(--fail);
		margin: 0;
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 4px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.btn.ghost {
		background: none;
		color: var(--mut);
	}
</style>
