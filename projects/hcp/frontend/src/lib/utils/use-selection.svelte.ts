import { SvelteSet } from "svelte/reactivity";

export function useSelection<T>(
  items: () => T[],
  key: (item: T) => string,
) {
  const selected = new SvelteSet<string>();

  const allSelected = $derived(
    items().length > 0 && items().every((i) => selected.has(key(i))),
  );

  function toggleAll() {
    if (allSelected) {
      selected.clear();
    } else {
      for (const item of items()) selected.add(key(item));
    }
  }

  function toggleOne(k: string) {
    if (selected.has(k)) {
      selected.delete(k);
    } else {
      selected.add(k);
    }
  }

  return {
    selected,
    get allSelected() {
      return allSelected;
    },
    toggleAll,
    toggleOne,
  };
}
