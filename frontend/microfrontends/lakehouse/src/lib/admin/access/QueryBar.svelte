<script lang="ts">
	// The one query surface. Three shapes, not three tabs: the shape switches the FIELDS, never the
	// view — the canvas underneath stays mounted and gets highlighted, which is the whole point of
	// replacing the old tab strip.
	//
	// Every field is a datalist-backed input driven by the compiled model, so a relation that does not
	// exist on the chosen type cannot be typed by accident. The old workbench took free text and
	// learned about a phantom relation only from the catalog's 400, one round trip later.
	import { Button } from '@rask/ui/button';
	import { Input } from '@rask/ui/input';
	import { Search } from '@lucide/svelte';
	import type { QueryKind } from './graph';

	type Props = {
		kind: QueryKind;
		user: string;
		relation: string;
		object: string;
		objectType: string;
		depth: number;
		busy: boolean;
		/** Model-derived suggestions — types, and the relations defined on the type in play. */
		types: readonly string[];
		relations: readonly string[];
		subjects: readonly string[];
		objects: readonly string[];
		onrun: (next: {
			kind: QueryKind;
			user: string;
			relation: string;
			object: string;
			objectType: string;
			depth: number;
		}) => void;
	};

	let {
		kind = $bindable(),
		user = $bindable(),
		relation = $bindable(),
		object = $bindable(),
		objectType = $bindable(),
		depth = $bindable(),
		busy,
		types,
		relations,
		subjects,
		objects,
		onrun,
	}: Props = $props();

	const SHAPES: { kind: QueryKind; label: string; hint: string }[] = [
		{ kind: 'what', label: 'What can…', hint: 'every object a subject reaches (ListObjects)' },
		{ kind: 'who', label: 'Who can…', hint: 'every subject holding a relation (ListUsers)' },
		{ kind: 'why', label: 'Why…', hint: 'the derivation, hop by hop (Check + Expand)' },
	];

	const ready = $derived.by(() => {
		if (kind === 'what') return !!user.trim() && !!relation.trim() && !!objectType.trim();
		if (kind === 'who') return !!object.trim() && !!relation.trim();
		return !!user.trim() && !!relation.trim() && !!object.trim();
	});

	const hint = $derived(SHAPES.find((s) => s.kind === kind)?.hint ?? '');

	function submit(event: SubmitEvent): void {
		event.preventDefault();
		if (!ready || busy) return;
		onrun({ kind, user, relation, object, objectType, depth });
	}
</script>

<form class="flex flex-col gap-2" onsubmit={submit}>
	<div class="flex flex-wrap items-center gap-1.5">
		{#each SHAPES as shape (shape.kind)}
			<Button
				type="button"
				size="sm"
				variant={kind === shape.kind ? 'default' : 'outline'}
				onclick={() => (kind = shape.kind)}
			>
				{shape.label}
			</Button>
		{/each}
		<span class="ml-1 text-xs text-muted-foreground">{hint}</span>
	</div>

	<div class="flex flex-wrap items-center gap-2">
		{#if kind === 'what' || kind === 'why'}
			<Input
				bind:value={user}
				list="access-subjects"
				class="w-56 font-mono text-xs"
				placeholder="subject — user:alice, team:eng#member"
				aria-label="Subject"
			/>
		{/if}

		<Input
			bind:value={relation}
			list="access-relations"
			class="w-44 font-mono text-xs"
			placeholder="relation — can_read_data"
			aria-label="Relation"
		/>

		{#if kind === 'what'}
			<Input
				bind:value={objectType}
				list="access-types"
				class="w-40 font-mono text-xs"
				placeholder="object type — table"
				aria-label="Object type"
			/>
		{:else}
			<Input
				bind:value={object}
				list="access-objects"
				class="w-64 font-mono text-xs"
				placeholder="object — table:bronze$pages"
				aria-label="Object"
			/>
		{/if}

		{#if kind === 'why'}
			<label class="flex items-center gap-1.5 text-xs text-muted-foreground">
				depth
				<Input
					type="number"
					min="1"
					max="6"
					bind:value={depth}
					class="w-16 font-mono text-xs"
					aria-label="Derivation depth"
				/>
			</label>
		{/if}

		<Button type="submit" size="sm" disabled={!ready || busy}>
			<Search size={13} />
			{busy ? 'Running…' : 'Run'}
		</Button>
	</div>

	<!-- Model-driven suggestions. Native datalist keeps the input free-text (a userset is not in any
	     list) while still making the legal values one keystroke away. -->
	<datalist id="access-types">
		{#each types as t (t)}<option value={t}></option>{/each}
	</datalist>
	<datalist id="access-relations">
		{#each relations as r (r)}<option value={r}></option>{/each}
	</datalist>
	<datalist id="access-subjects">
		{#each subjects as s (s)}<option value={s}></option>{/each}
	</datalist>
	<datalist id="access-objects">
		{#each objects as o (o)}<option value={o}></option>{/each}
	</datalist>
</form>
