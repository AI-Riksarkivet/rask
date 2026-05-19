<script lang="ts">
  import { Button, Dialog, Card } from "@your-repo/oxen";
  let open = $state(false);
</script>

<main class="mx-auto max-w-2xl space-y-6 p-8">
  <h1 class="text-3xl font-bold">Webapp</h1>

  <Card class="p-6 space-y-4">
    <p class="text-muted-foreground">Components from <code>@your-repo/oxen</code>.</p>
    <div class="flex gap-2">
      <Button>Default</Button>
      <Button variant="outline">Outline</Button>
      <Button variant="destructive">Destructive</Button>
    </div>
  </Card>

  <Dialog.Root bind:open>
    <Dialog.Trigger>
      {#snippet child({ props })}
        <Button {...props}>Open dialog</Button>
      {/snippet}
    </Dialog.Trigger>
    <Dialog.Content>
      <Dialog.Title>Confirm action</Dialog.Title>
      <Dialog.Description>Wrapping Bits UI primitives with our tokens.</Dialog.Description>
      <div class="flex justify-end gap-2 pt-4">
        <Button variant="outline" onclick={() => (open = false)}>Cancel</Button>
        <Button onclick={() => (open = false)}>Confirm</Button>
      </div>
    </Dialog.Content>
  </Dialog.Root>
</main>
