<script lang="ts">
  import type { HTMLAttributes } from "svelte/elements";
  import { cn } from "../../utils/cn.js";

  let {
    class: className,
    children,
    ...rest
  }: HTMLAttributes<HTMLDivElement> = $props();
</script>

<div
  class={cn("rounded-lg border border-border bg-card text-card-foreground shadow-sm", className)}
  {...rest}
>
  {@render children?.()}
</div>
