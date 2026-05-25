<script context="module" lang="ts">
  import { defineMeta } from "@storybook/addon-svelte-csf";
  const { Story } = defineMeta({ title: "Welcome", tags: ["autodocs"] });
</script>

<Story name="Intro">
  <div class="prose p-8">
    <h1>Oxen Components</h1>
    <p>Shared Svelte 5 component library for the monorepo, built on Bits UI + Tailwind 4.</p>
    <p>Browse the sidebar to see Button, Dialog, and Card stories.</p>
  </div>
</Story>
