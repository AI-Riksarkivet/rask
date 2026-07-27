import type { Component, ComponentProps, Snippet } from 'svelte';

/** Marks a Svelte COMPONENT in a `columnDef.cell`/`header` — `FlexRender` instantiates it. */
export class RenderComponentConfig<TComponent extends Component> {
	component: TComponent;
	props: ComponentProps<TComponent> | Record<string, never>;
	constructor(
		component: TComponent,
		props: ComponentProps<TComponent> | Record<string, never> = {},
	) {
		this.component = component;
		this.props = props;
	}
}

/** Marks a Svelte SNIPPET in a `columnDef.cell`/`header` — `FlexRender` renders it. */
export class RenderSnippetConfig<TProps> {
	snippet: Snippet<[TProps]>;
	params: TProps;
	constructor(snippet: Snippet<[TProps]>, params: TProps) {
		this.snippet = snippet;
		this.params = params;
	}
}

/**
 * Render a Svelte component from a ColumnDef `cell`/`header`, e.g.
 * `header: ({ column }) => renderComponent(DataTableHeaderButton, { label: 'Name', … })`.
 * Use `renderSnippet` for snippets.
 */
export function renderComponent<
	// Component is contravariant in its props — `any` is the only sound bound here.
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	T extends Component<any>,
	Props extends ComponentProps<T>,
>(component: T, props: Props = {} as Props) {
	return new RenderComponentConfig(component, props);
}

/**
 * Render a one-parameter Svelte snippet from a ColumnDef `cell`/`header`, e.g.
 * `cell: ({ row }) => renderSnippet(nameCell, row.original)`.
 * Use `renderComponent` for components.
 */
export function renderSnippet<TProps>(snippet: Snippet<[TProps]>, params: TProps = {} as TProps) {
	return new RenderSnippetConfig(snippet, params);
}
