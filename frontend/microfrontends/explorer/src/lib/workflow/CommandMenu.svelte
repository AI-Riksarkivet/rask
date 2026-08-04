<script lang="ts">
	/**
	 * ⌘K command palette for the workflow editor (bits-ui Command in a Dialog).
	 *
	 * One searchable place that DOCUMENTS every shortcut and RUNS the action:
	 * the run commands (whole graph / selected node / branch fresh), the edit
	 * commands (undo/redo/copy/paste/delete/tidy/reset), each with its key
	 * combo as a chip. Node-scoped commands need exactly one selected node.
	 * The non-interactive "On a node card" group explains the hover actions
	 * (▶ / Shift+▶ / right-click) that have no global key equivalent.
	 */
	import { Command, Dialog } from 'bits-ui';
	import {
		Copy,
		ClipboardPaste,
		Eraser,
		Play,
		RefreshCw,
		RotateCcw,
		Redo2,
		Trash2,
		Undo2,
		Wand2,
	} from '@lucide/svelte';
	import { graph, nodeLabel } from '$lib/workflow/graph.svelte';
	import { commandMenu } from '$lib/workflow/command-menu.svelte';

	/** The single selected node (node-scoped run commands), or null. */
	const selectedId = $derived(
		graph.selectedNodeIds.length === 1 ? (graph.selectedNodeIds[0] ?? null) : null,
	);
	const selectedTitle = $derived.by(() => {
		if (!selectedId) return null;
		const kind = graph.kindOf(selectedId);
		return graph.config[selectedId]?.label?.trim() || (kind ? nodeLabel(kind) : selectedId);
	});

	function onKeydown(e: KeyboardEvent): void {
		if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
			e.preventDefault();
			commandMenu.toggle();
		}
	}

	/** Close the palette, then run — so run state renders on an unobscured canvas. */
	function act(fn: () => void): void {
		commandMenu.open = false;
		fn();
	}

	const ITEM =
		'flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none ' +
		'data-selected:bg-secondary data-disabled:cursor-default data-disabled:opacity-40';
	const CHIP =
		'ml-auto shrink-0 rounded-md border border-border bg-muted px-1.5 py-0.5 font-mono text-[0.7rem] text-muted-foreground';
	const HEADING = 'px-2 pt-2 pb-1 text-[0.7rem] tracking-wide text-muted-foreground uppercase';
</script>

<svelte:window onkeydown={onKeydown} />

<Dialog.Root bind:open={commandMenu.open}>
	<Dialog.Portal>
		<Dialog.Overlay class="fixed inset-0 z-50 bg-black/40" />
		<Dialog.Content
			class="border-border bg-card fixed top-[20%] left-1/2 z-50 w-[34rem] max-w-[calc(100vw-2rem)] -translate-x-1/2 overflow-hidden rounded-lg border shadow-xl"
		>
			<Dialog.Title class="sr-only">Workflow commands</Dialog.Title>
			<Command.Root class="flex flex-col">
				<Command.Input
					placeholder="Type a command…"
					class="border-border text-foreground placeholder:text-muted-foreground w-full border-b bg-transparent px-3 py-2.5 text-sm focus:outline-none"
				/>
				<Command.List class="max-h-[22rem] overflow-y-auto p-1.5">
					<Command.Viewport>
						<Command.Empty class="text-muted-foreground px-2 py-6 text-center text-xs">
							No matching command.
						</Command.Empty>

						<Command.Group>
							<Command.GroupHeading class={HEADING}>Run</Command.GroupHeading>
							<Command.GroupItems>
								<Command.Item
									value="run workflow all"
									disabled={graph.running}
									onSelect={() => act(() => void graph.run())}
									class={ITEM}
								>
									<Play class="size-3.5" /> Run workflow (all nodes, fresh)
								</Command.Item>
								<Command.Item
									value="run selected node"
									keywords={['play', 'execute', 'partial']}
									disabled={graph.running || !selectedId}
									onSelect={() => act(() => void graph.runNode(selectedId!))}
									class={ITEM}
								>
									<Play class="size-3.5" />
									Run selected node{selectedTitle ? ` — ${selectedTitle}` : ''}
									<span class={CHIP}>▶ on node</span>
								</Command.Item>
								<Command.Item
									value="run selected branch fresh"
									keywords={['rerun', 'upstream', 'force']}
									disabled={graph.running || !selectedId}
									onSelect={() => act(() => void graph.runNode(selectedId!, { fresh: true }))}
									class={ITEM}
								>
									<RefreshCw class="size-3.5" />
									Run selected branch fresh{selectedTitle ? ` — ${selectedTitle}` : ''}
									<span class={CHIP}>Shift+▶</span>
								</Command.Item>
								<Command.Item
									value="clear run results"
									disabled={graph.running}
									onSelect={() => act(() => graph.resetRun())}
									class={ITEM}
								>
									<Eraser class="size-3.5" /> Clear run results
								</Command.Item>
							</Command.GroupItems>
						</Command.Group>

						<Command.Separator class="bg-border my-1 h-px" />

						<Command.Group>
							<Command.GroupHeading class={HEADING}>Edit</Command.GroupHeading>
							<Command.GroupItems>
								<Command.Item
									value="undo"
									disabled={!graph.canUndo || graph.running}
									onSelect={() => act(() => graph.undo())}
									class={ITEM}
								>
									<Undo2 class="size-3.5" /> Undo <span class={CHIP}>Ctrl/⌘ Z</span>
								</Command.Item>
								<Command.Item
									value="redo"
									disabled={!graph.canRedo || graph.running}
									onSelect={() => act(() => graph.redo())}
									class={ITEM}
								>
									<Redo2 class="size-3.5" /> Redo <span class={CHIP}>Ctrl/⌘ ⇧ Z</span>
								</Command.Item>
								<Command.Item
									value="copy selection"
									disabled={!graph.hasSelection}
									onSelect={() => act(() => graph.copySelection())}
									class={ITEM}
								>
									<Copy class="size-3.5" /> Copy selection <span class={CHIP}>Ctrl/⌘ C</span>
								</Command.Item>
								<Command.Item
									value="paste"
									disabled={graph.running}
									onSelect={() => act(() => graph.paste())}
									class={ITEM}
								>
									<ClipboardPaste class="size-3.5" /> Paste <span class={CHIP}>Ctrl/⌘ V</span>
								</Command.Item>
								<Command.Item
									value="delete selection"
									disabled={!graph.hasSelection || graph.running}
									onSelect={() => act(() => graph.deleteSelected())}
									class={ITEM}
								>
									<Trash2 class="size-3.5" /> Delete selection <span class={CHIP}>⌫</span>
								</Command.Item>
								<Command.Item
									value="tidy auto layout"
									disabled={graph.running}
									onSelect={() => act(() => graph.tidy())}
									class={ITEM}
								>
									<Wand2 class="size-3.5" /> Tidy (auto-layout)
								</Command.Item>
								<Command.Item
									value="reset graph to starter"
									disabled={graph.running}
									onSelect={() => act(() => graph.reset())}
									class={ITEM}
								>
									<RotateCcw class="size-3.5" /> Reset to starter graph
								</Command.Item>
							</Command.GroupItems>
						</Command.Group>

						<Command.Separator class="bg-border my-1 h-px" />

						<!-- Pure documentation: node-card hover actions with no global key. -->
						<Command.Group>
							<Command.GroupHeading class={HEADING}>On a node card</Command.GroupHeading>
							<Command.GroupItems>
								<Command.Item value="hint play node" disabled forceMount class={ITEM}>
									▶ — run that node; upstream results are reused, missing upstream runs once
								</Command.Item>
								<Command.Item value="hint shift play branch" disabled forceMount class={ITEM}>
									Shift+▶ — re-execute the node and its whole upstream branch
								</Command.Item>
								<Command.Item value="hint stale badge" disabled forceMount class={ITEM}>
									amber “stale” — upstream re-ran since this node's results; ▶ refreshes
								</Command.Item>
								<Command.Item value="hint right click menu" disabled forceMount class={ITEM}>
									right-click — run / duplicate / disable / delete; right-click canvas adds a node
								</Command.Item>
							</Command.GroupItems>
						</Command.Group>
					</Command.Viewport>
				</Command.List>
			</Command.Root>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
