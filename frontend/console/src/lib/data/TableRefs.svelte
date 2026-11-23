<script lang="ts">
	// A table's refs as Lance has them, under its commit log (#113): its branches (create from a version,
	// delete) and its tags (move to another version, delete). Both deletes are destructive and ask first.
	// Its drafts, armed confirms and the one write in flight are its own; a landed write asks the page to
	// read its aggregate again, which is where the tags and branches come from.
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import { Select } from '@riksarkivet/ui/select';
	import type { CatalogResult } from './catalog';
	import { fmtBytes } from './history';
	import {
		createTableBranch,
		deleteTableBranch,
		deleteTableTag,
		moveTableTag,
	} from './remote/catalog.api';

	let {
		table,
		tags,
		branches,
		versionOptions,
		onchange,
	}: {
		table: string;
		tags: [string, { version?: number; branch?: string | null }][];
		branches: [string, { createAt?: number; manifestSize?: number | null }][];
		/** The versions a branch may start from or a tag may move to, as select options. */
		versionOptions: { value: string; label: string }[];
		onchange: () => void;
	} = $props();

	let refBusy = $state(false);
	let refError = $state<string | null>(null);
	let refConfirm = $state<{ kind: 'tag' | 'branch'; name: string } | null>(null);
	let movingTag = $state<string | null>(null);
	let moveTo = $state('');
	let newBranch = $state('');
	let newBranchFrom = $state('');

	async function refDo(fn: () => Promise<CatalogResult<unknown>>): Promise<void> {
		if (refBusy) return;
		refBusy = true;
		refError = null;
		try {
			const res = await fn();
			if (res.ok) onchange();
			else if (res.status === 401) refError = 'Sign in to manage tags & branches.';
			else if (res.status === 403) refError = 'Denied: managing refs needs writer/owner access.';
			else refError = res.detail;
		} finally {
			refBusy = false;
			refConfirm = null;
		}
	}

	async function runMoveTag(): Promise<void> {
		if (!movingTag || !moveTo) return;
		const name = movingTag;
		await refDo(() => moveTableTag({ table, tag: name, version: Number(moveTo) }));
		movingTag = null;
		moveTo = '';
	}

	async function runCreateBranch(): Promise<void> {
		const name = newBranch.trim();
		if (!name) return;
		await refDo(() =>
			createTableBranch({ table, name, fromVersion: newBranchFrom ? Number(newBranchFrom) : null }),
		);
		newBranch = '';
		newBranchFrom = '';
	}
</script>

<h3>Branches</h3>
<div class="refs">
	<span class="chip mono">main</span>
	{#each branches as [name, b] (name)}
		<span class="chip branch mono"
			>{name}<span class="mut"> · {fmtBytes(b.manifestSize)}</span>
			{#if refConfirm?.kind === 'branch' && refConfirm.name === name}
				<Button
					variant="destructive"
					size="xs"
					aria-label="confirm delete branch {name}"
					disabled={refBusy}
					onclick={() => refDo(() => deleteTableBranch({ table, name }))}>delete?</Button
				>
				<Button
					variant="ghost"
					size="icon-xs"
					aria-label="cancel"
					onclick={() => (refConfirm = null)}>×</Button
				>
			{:else}
				<Button
					variant="ghost"
					size="icon-xs"
					title="delete branch"
					aria-label="delete branch {name}"
					disabled={refBusy}
					onclick={() => (refConfirm = { kind: 'branch', name })}>×</Button
				>
			{/if}
		</span>
	{/each}
</div>
<form
	class="row"
	onsubmit={(e) => {
		e.preventDefault();
		runCreateBranch();
	}}
>
	<Input
		class="h-7 w-44 font-mono text-xs"
		bind:value={newBranch}
		placeholder="new branch"
		aria-label="New branch name"
	/>
	<Select
		bind:value={newBranchFrom}
		ariaLabel="Branch from version"
		placeholder="from version (latest)"
		options={versionOptions}
	/>
	<Button size="sm" type="submit" disabled={refBusy || !newBranch.trim()}>Create branch</Button>
</form>

<h3>Tags</h3>
<div class="refs">
	{#if tags.length === 0}
		<span class="mut">No tags: a tag pins one version of the table by a name you choose.</span>
	{:else}
		{#each tags as [name, t] (name)}
			{#if movingTag === name}
				<span class="chip tag mono">
					{name} →
					<span class="movesel">
						<Select
							bind:value={moveTo}
							ariaLabel="move {name} to version"
							placeholder="version"
							options={versionOptions}
						/>
					</span>
					<Button variant="ghost" size="xs" disabled={refBusy || !moveTo} onclick={runMoveTag}
						>save</Button
					>
					<Button
						variant="ghost"
						size="icon-xs"
						aria-label="cancel move"
						onclick={() => (movingTag = null)}>×</Button
					>
				</span>
			{:else}
				<span class="chip tag mono"
					>{name} → v{t.version}{#if t.branch}&nbsp;on {t.branch}{/if}
					<Button
						variant="ghost"
						size="icon-xs"
						title="move tag"
						aria-label="move tag {name}"
						disabled={refBusy}
						onclick={() => {
							movingTag = name;
							moveTo = '';
						}}>↪</Button
					>
					{#if refConfirm?.kind === 'tag' && refConfirm.name === name}
						<Button
							variant="destructive"
							size="xs"
							aria-label="confirm delete tag {name}"
							disabled={refBusy}
							onclick={() => refDo(() => deleteTableTag({ table, tag: name }))}>delete?</Button
						>
						<Button
							variant="ghost"
							size="icon-xs"
							aria-label="cancel"
							onclick={() => (refConfirm = null)}>×</Button
						>
					{:else}
						<Button
							variant="ghost"
							size="icon-xs"
							title="delete tag"
							aria-label="delete tag {name}"
							disabled={refBusy}
							onclick={() => (refConfirm = { kind: 'tag', name })}>×</Button
						>
					{/if}
				</span>
			{/if}
		{/each}
	{/if}
</div>
{#if refError}<p class="error">{refError}</p>{/if}

<style>
	h3 {
		font-size: 11px;
		font-weight: 600;
		margin: 16px 0 6px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
		margin: 0 0 8px;
	}
	.refs {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-bottom: 8px;
	}
	.row {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 8px;
	}
	.chip {
		align-items: center;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		display: inline-flex;
		font-size: 12px;
		gap: 4px;
		padding: 1px 4px 1px 8px;
	}
	.movesel {
		display: inline-flex;
	}
	.error {
		color: var(--destructive);
		font-size: 12px;
		margin-top: 8px;
	}
	.mono {
		font-family: var(--font-mono, ui-monospace, monospace);
	}
</style>
