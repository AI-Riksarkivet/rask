import { describe, expect, it } from 'vitest';
import type { Component } from 'svelte';
import { mergeObjects } from '../src/lib/components/data-table/create-svelte-table.svelte';
import {
	RenderComponentConfig,
	RenderSnippetConfig,
	renderComponent,
	renderSnippet,
} from '../src/lib/components/data-table/render-helpers';
import { selectionColumn } from '../src/lib/components/data-table/selection-column';

// Pure helpers of the data-table stack (the reactive createSvelteTable itself needs a component
// context and is exercised by the consuming zones' e2e).
describe('mergeObjects', () => {
	it('later sources win and getter semantics are preserved (read on ACCESS, not merged eagerly)', () => {
		let hits = 0;
		const merged = mergeObjects(
			{ a: 1, b: 1 },
			{
				b: 2,
				get c() {
					hits += 1;
					return hits;
				},
			},
		);
		expect(merged.a).toBe(1);
		expect(merged.b).toBe(2);
		expect(hits).toBe(0); // the getter has not run yet
		expect(merged.c).toBe(1);
		expect(merged.c).toBe(2); // re-read → re-evaluated, exactly the reactivity seam tanstack needs
	});

	it('resolves thunk sources lazily and tolerates null-returning thunks', () => {
		const merged = mergeObjects(
			{ a: 1 },
			() => null,
			() => ({ b: 2 }),
		);
		expect(merged.a).toBe(1);
		expect(merged.b).toBe(2);
		expect('b' in merged).toBe(true);
		expect(Object.keys(merged).sort()).toEqual(['a', 'b']);
	});
});

describe('render helpers', () => {
	const FakeComponent = (() => {}) as unknown as Component<{ label: string }>;

	it('renderComponent wraps a component + props in a RenderComponentConfig', () => {
		const cfg = renderComponent(FakeComponent, { label: 'Name' });
		expect(cfg).toBeInstanceOf(RenderComponentConfig);
		expect(cfg.component).toBe(FakeComponent);
		expect(cfg.props).toEqual({ label: 'Name' });
	});

	it('renderSnippet wraps a snippet + params in a RenderSnippetConfig', () => {
		const snippet = (() => {}) as unknown as Parameters<typeof renderSnippet<string>>[0];
		const cfg = renderSnippet(snippet, 'row-1');
		expect(cfg).toBeInstanceOf(RenderSnippetConfig);
		expect(cfg.snippet).toBe(snippet);
		expect(cfg.params).toBe('row-1');
	});
});

describe('selectionColumn', () => {
	it('builds the fixed-width, unsortable select column with header + cell renderers', () => {
		const col = selectionColumn<{ id: string }>();
		expect(col.id).toBe('select');
		expect(col.enableSorting).toBe(false);
		expect(col.enableHiding).toBe(false);
		expect(col.meta?.headerClass).toBe('w-10');
		expect(typeof col.header).toBe('function');
		expect(typeof col.cell).toBe('function');
	});

	it('wires the cell checkbox to the row selection API', () => {
		let toggled: boolean | undefined;
		const row = {
			getIsSelected: () => false,
			toggleSelected: (v: boolean) => {
				toggled = v;
			},
		};
		const col = selectionColumn<{ id: string }>();
		const cfg = (col.cell as (ctx: { row: typeof row }) => RenderComponentConfig<Component>)({
			row,
		});
		expect(cfg).toBeInstanceOf(RenderComponentConfig);
		const props = cfg.props as { checked: boolean; onCheckedChange: (v: boolean) => void };
		expect(props.checked).toBe(false);
		props.onCheckedChange(true);
		expect(toggled).toBe(true);
	});
});
