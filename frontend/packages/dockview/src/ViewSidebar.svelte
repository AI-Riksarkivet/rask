<script lang="ts">
	/**
	 * The saved-views sidebar — select a view, or create one from the current arrangement.
	 *
	 * The modified marker beside the active view is the visible half of the decision `DockViews` makes:
	 * loading a view does NOT redirect autosave at it. Autosave keeps writing the implicit draft, and
	 * the view stays frozen until you press Save. Overwriting on every drag would mean you could never
	 * open a saved arrangement to LOOK at it without destroying it.
	 */
	import { Check, Plus, Save, Trash2 } from '@lucide/svelte';
	import type { DockViews } from './views.svelte';
	import type { SerializedDockview } from 'dockview';

	interface Props {
		views: DockViews<SerializedDockview>;
		onselect: (id: string) => void;
		/** True when rendered INSIDE the shared shell rail (AppShell sidebarContent): drops this
		 *  component's own aside chrome — width, border — and lets the rail's own geometry rule. */
		rail?: boolean;
	}
	let { views, onselect, rail = false }: Props = $props();

	let creating = $state(false);
	let draftName = $state('');

	async function create(): Promise<void> {
		if (draftName.trim() === '') return;
		if (await views.saveAs(draftName)) {
			draftName = '';
			creating = false;
		}
	}
</script>

<aside class="views" class:rail>
	<header>
		<span class="heading">Views</span>
		<button
			type="button"
			class="icon"
			title="Save the current arrangement as a new view"
			aria-label="New view"
			onclick={() => (creating = !creating)}
		>
			<Plus size={14} />
		</button>
	</header>

	{#if creating}
		<form
			class="new"
			onsubmit={(e) => {
	e.preventDefault();
	void create();
}}
		>
			<!-- svelte-ignore a11y_autofocus -- the field appears on an explicit click, so focusing it is
			     what the click asked for. -->
			<input
				bind:value={draftName}
				type="text"
				placeholder="Name this view…"
				aria-label="View name"
				autofocus
			/>
		</form>
	{/if}

	{#if views.lastError !== null}
		<p class="problem">{views.lastError}</p>
	{/if}

	{#if views.phase === 'unreadable'}
		<!-- Never rendered as "no saved views": that reads as empty and invites the user to recreate
		     work that is still there. -->
		<p class="problem">
			Your saved views could not be read ({views.detail}). They have NOT been changed.
		</p>
	{:else if views.views.length === 0}
		<p class="empty">No saved views yet. Arrange the dock, then press +.</p>
	{:else}
		<ul>
			{#each views.views as v (v.id)}
				{@const isActive = v.id === views.activeId}
				<li>
					<button type="button" class="row" class:on={isActive} onclick={() => onselect(v.id)}>
						{#if isActive}<Check size={13} />{/if}
						<span class="name">{v.name}</span>
						<!-- The whole point of the state model, in one dot. -->
						{#if isActive && views.dirty}<span class="dot" title="Modified since it was saved"
						  >●</span
						>{/if}
					</button>
					{#if isActive}
						<button
							type="button"
							class="icon"
							title="Save the current arrangement into this view"
							aria-label="Save view"
							disabled={!views.dirty || views.busy}
							onclick={() => void views.save()}
						>
							<Save size={13} />
						</button>
					{/if}
					<button
						type="button"
						class="icon"
						title="Delete this view"
						aria-label="Delete view"
						disabled={views.busy}
						onclick={() => void views.remove(v.id)}
					>
						<Trash2 size={13} />
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</aside>

<style>
	.views {
		display: flex;
		flex: 0 0 auto;
		flex-direction: column;
		gap: 4px;
		width: 200px;
		padding: 8px;
		border-inline-end: 1px solid var(--border);
		background: var(--background);
		overflow-y: auto;
	}
	.views.rail {
		width: 100%;
		border-inline-end: none;
		background: transparent;
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.heading {
		font-size: 11px;
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted-foreground);
	}
	ul {
		display: flex;
		flex-direction: column;
		gap: 1px;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	li {
		display: flex;
		align-items: center;
		gap: 2px;
	}
	.row {
		display: flex;
		flex: 1 1 auto;
		align-items: center;
		gap: 6px;
		min-width: 0;
		padding: 5px 6px;
		border: none;
		border-radius: calc(var(--radius) - 6px);
		background: transparent;
		color: var(--foreground);
		font-size: 12px;
		text-align: start;
		cursor: pointer;
	}
	.row:hover {
		background: var(--accent);
	}
	.row.on {
		color: var(--primary);
	}
	.row:focus-visible,
	.icon:focus-visible,
	input:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.dot {
		flex: 0 0 auto;
		font-size: 9px;
		color: var(--warning, var(--muted-foreground));
	}
	.icon {
		display: grid;
		place-items: center;
		width: 22px;
		height: 22px;
		border: none;
		border-radius: calc(var(--radius) - 6px);
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.icon:hover:not(:disabled) {
		color: var(--foreground);
		background: var(--accent);
	}
	.icon:disabled {
		opacity: 0.35;
		cursor: default;
	}
	input {
		width: 100%;
		padding: 4px 6px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 6px);
		background: var(--background);
		color: var(--foreground);
		font-size: 12px;
	}
	.empty,
	.problem {
		margin: 4px 2px;
		font-size: 11px;
		color: var(--muted-foreground);
	}
	.problem {
		color: var(--destructive);
	}
</style>
