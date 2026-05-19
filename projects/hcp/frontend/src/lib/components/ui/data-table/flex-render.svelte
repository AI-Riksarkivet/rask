<script
	lang="ts"
	generics="TData, TValue, TContext extends HeaderContext<TData, TValue> | CellContext<TData, TValue>"
>
	import type { CellContext, ColumnDefTemplate, HeaderContext } from '@tanstack/table-core';
	import { RenderComponentConfig, RenderSnippetConfig } from './render-helpers.js';

	type Props = {
		content?: TContext extends HeaderContext<TData, TValue>
			? ColumnDefTemplate<HeaderContext<TData, TValue>>
			: TContext extends CellContext<TData, TValue>
				? ColumnDefTemplate<CellContext<TData, TValue>>
				: never;
		context: TContext;
	};

	let { content, context }: Props = $props();

	function resolve(c: Props['content'], ctx: TContext) {
		if (typeof c === 'string') return { kind: 'text' as const, value: c };
		if (c instanceof Function) {
			const result = (c as (props: TContext) => unknown)(ctx);
			if (result instanceof RenderComponentConfig) return { kind: 'component' as const, result };
			if (result instanceof RenderSnippetConfig) return { kind: 'snippet' as const, result };
			return { kind: 'text' as const, value: String(result ?? '') };
		}
		return { kind: 'text' as const, value: '' };
	}
</script>

{#if content != null}
	{@const resolved = resolve(content, context)}
	{#if resolved.kind === 'component'}
		{@const { component: Component, props } = resolved.result}
		<Component {...props} />
	{:else if resolved.kind === 'snippet'}
		{@render resolved.result.snippet(resolved.result.params)}
	{:else}
		{resolved.value}
	{/if}
{/if}
