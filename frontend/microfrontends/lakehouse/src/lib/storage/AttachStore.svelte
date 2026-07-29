<script lang="ts">
	// Attach a bucket so it can be BROWSED. This registers a location; it reads nothing, copies
	// nothing and ingests nothing — which is why it lives in the storage view and not near the
	// cascade. The catalog is the authority: it gates on estate-admin, forces `read_only`, refuses a
	// duplicate name, and returns the whole registry, which is what this re-renders from.
	import { Plus, X } from '@lucide/svelte';
	import { attachStore, type StorageRole, type Store, type StoreDraft } from './storage';

	let { onattached }: { onattached: (stores: Store[]) => void } = $props();

	const ROLES: StorageRole[] = ['raw', 'bronze', 'silver', 'gold', 'derived', 'observability'];

	let open = $state(false);
	let saving = $state(false);
	let error = $state('');
	let draft = $state<StoreDraft>({
		name: '',
		bucket: '',
		role: 'raw',
		endpoint: '',
		description: '',
	});

	// `raw` by default, not `bronze`: an attached bucket is by definition something the estate did not
	// produce, and the governed tiers are written by the cascade. Defaulting to a tier would invite
	// mislabelling external data as governed, which the tier view then reports as fact.
	function reset(): void {
		draft = { name: '', bucket: '', role: 'raw', endpoint: '', description: '' };
		error = '';
	}

	async function submit(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		if (saving) return;
		saving = true;
		error = '';
		const res = await attachStore(draft);
		saving = false;
		if (!res.ok) {
			// The server's own detail, not a generic message: 403 (not estate-admin), 409 (name taken)
			// and 503 (state store unreachable) are different problems with different fixes, and only
			// the server knows which happened.
			error =
				res.detail ||
				(res.status === 403
					? 'You are not an estate admin, so you cannot attach a store.'
					: `Attach failed (HTTP ${res.status}).`);
			return;
		}
		onattached(res.data.stores);
		open = false;
		reset();
	}
</script>

{#if !open}
	<button class="btn" onclick={() => (open = true)}>
		<Plus size={13} /> Attach store
	</button>
{:else}
	<form class="attach" onsubmit={submit}>
		<header>
			<h3>Attach a store</h3>
			<button
				type="button"
				class="icon"
				aria-label="Cancel"
				onclick={() => {
					open = false;
					reset();
				}}><X size={14} /></button
			>
		</header>

		<label>
			<span>name</span>
			<input bind:value={draft.name} required placeholder="scans-2024" autocomplete="off" />
		</label>
		<label>
			<span>bucket</span>
			<input bind:value={draft.bucket} required placeholder="scans-2024" autocomplete="off" />
		</label>
		<label>
			<span>endpoint</span>
			<!-- Blank means "this deployment's own S3". Spelling that out matters: the governed tiers all
			     use it, and an operator typing the warehouse URL by hand would pin a value that should
			     follow the deployment. -->
			<input
				bind:value={draft.endpoint}
				placeholder="https://s3.example.org — blank = this deployment's default"
				autocomplete="off"
			/>
		</label>
		<label>
			<span>role</span>
			<select bind:value={draft.role}>
				{#each ROLES as role (role)}
					<option value={role}>{role}</option>
				{/each}
			</select>
		</label>
		<label>
			<span>description</span>
			<input bind:value={draft.description} placeholder="What a reader should expect to find" />
		</label>

		<p class="note">
			Registers a location for browsing — nothing is read, copied or ingested. Attached stores are
			read-only. Credentials come from this deployment; a bucket needing different ones will fail to
			list until secret references land.
		</p>

		{#if error}<p class="error">{error}</p>{/if}

		<button class="btn primary" type="submit" disabled={saving}>
			{saving ? 'Attaching…' : 'Attach'}
		</button>
	</form>
{/if}

<style>
	.attach {
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		background: var(--panel);
		padding: 12px 14px;
		display: grid;
		gap: 8px;
		max-width: 520px;
	}
	header {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	h3 {
		font-size: 13px;
		margin: 0;
		flex: 1;
	}
	label {
		display: grid;
		grid-template-columns: 7rem 1fr;
		align-items: center;
		gap: 10px;
		font-size: 12px;
	}
	label span {
		color: var(--faint);
	}
	input,
	select {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 5px 8px;
		font-size: 12px;
		min-width: 0;
	}
	.note {
		color: var(--faint);
		font-size: 11px;
		margin: 2px 0 0;
		line-height: 1.45;
	}
	.error {
		color: var(--fail);
		font-size: 12px;
		margin: 0;
	}
	.icon {
		background: none;
		border: none;
		color: var(--mut);
		cursor: pointer;
		padding: 2px;
		display: inline-flex;
	}
	.btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		width: max-content;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 5px 10px;
		font-size: 12px;
		cursor: pointer;
	}
	.btn.primary {
		background: var(--ok);
		border-color: var(--ok);
		color: var(--panel);
	}
	.btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
</style>
