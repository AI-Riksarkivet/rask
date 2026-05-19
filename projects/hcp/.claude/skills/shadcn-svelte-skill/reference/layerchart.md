# LayerChart v2 Reference (Svelte 5)

Composable Svelte chart components for data visualization, integrated with shadcn-svelte.

**Package:** `layerchart@next` (v2 pre-release, Svelte 5)
**Docs:** https://next.layerchart.com (NOT layerchart.com — that's Svelte 4)
**shadcn-svelte integration:** https://www.shadcn-svelte.com/docs/components/chart

## Table of Contents

1. [Installation](#installation)
2. [Architecture](#architecture)
3. [shadcn-svelte Chart Integration](#shadcn-svelte-chart-integration)
4. [Simplified Charts (Quick Start)](#simplified-charts)
5. [Composable Charts (Full Control)](#composable-charts)
6. [Tooltips](#tooltips)
7. [Chart Types Gallery](#chart-types-gallery)
8. [Theming and Colors](#theming-and-colors)
9. [Storybook Stories](#storybook-stories)
10. [Component Reference](#component-reference)

## Installation

```bash
# Install LayerChart v2 (next tag is CRITICAL — stable 1.x is Svelte 4 only)
deno add -D npm:layerchart@next npm:d3-scale

# Add the shadcn-svelte chart wrapper component
deno run -A npm:shadcn-svelte@latest add chart
```

### CSS Setup

Add chart color variables to `src/app.css`:

```css
:root {
  --chart-1: oklch(0.646 0.222 41.116);
  --chart-2: oklch(0.6 0.118 184.704);
  --chart-3: oklch(0.398 0.07 227.392);
  --chart-4: oklch(0.828 0.189 84.429);
  --chart-5: oklch(0.769 0.188 70.08);
}

.dark {
  --chart-1: oklch(0.488 0.243 264.376);
  --chart-2: oklch(0.696 0.17 162.48);
  --chart-3: oklch(0.769 0.188 70.08);
  --chart-4: oklch(0.627 0.265 303.9);
  --chart-5: oklch(0.645 0.246 16.439);
}

@theme inline {
  --color-chart-1: var(--chart-1);
  --color-chart-2: var(--chart-2);
  --color-chart-3: var(--chart-3);
  --color-chart-4: var(--chart-4);
  --color-chart-5: var(--chart-5);
}
```

Or use the simplified CSS import (v2.0.0-next.39+):

```css
@import 'layerchart/shadcn-svelte.css';
```

## Architecture

LayerChart has two levels of abstraction:

### Simplified Charts

Pre-composed chart components that handle common scaffolding:

`BarChart`, `LineChart`, `AreaChart`, `PieChart`, `ScatterChart`, `ArcChart`

These wrap the lower-level primitives and provide props for common config (data, axes, tooltips, legends).

### Composable Primitives

Low-level building blocks for full control:

- **Chart**: The root container (establishes scales and context)
- **Layers**: `Svg`, `Html`, `Canvas` — rendering contexts
- **Marks**: `Area`, `Bars`, `Spline`, `Points`, `Pie`, `Labels` — data-driven visuals
- **Common**: `Axis`, `Grid`, `Legend`, `Frame`, `Rule` — chart furniture
- **Interactions**: `Tooltip`, `TooltipContext`, `Highlight`, `BrushContext`, `TransformContext`
- **Layout**: `Treemap`, `Sankey`, `Pack`, `Tree`, `Dagre`, `ForceSimulation`
- **Geo**: `GeoContext`, `GeoPath`, `GeoPoint`, `Graticule`, `TileImage`
- **Fill**: `LinearGradient`, `RadialGradient`, `Pattern`

## shadcn-svelte Chart Integration

shadcn-svelte does **not** wrap LayerChart — you use LayerChart components directly and only bring in the shadcn `Chart` wrapper for consistent tooltip styling:

```svelte
<script lang="ts">
  import * as Chart from "$lib/components/ui/chart/index.js";
  import { BarChart } from "layerchart";

  const data = [
    { month: "January", desktop: 186, mobile: 80 },
    { month: "February", desktop: 305, mobile: 200 },
    { month: "March", desktop: 237, mobile: 120 },
  ];
</script>

<Chart.Container>
  <BarChart {data} x="month" series={["desktop", "mobile"]}>
    {#snippet tooltip()}
      <Chart.Tooltip />
    {/snippet}
  </BarChart>
</Chart.Container>
```

The `Chart.Container` provides consistent sizing and the `Chart.Tooltip` matches your shadcn theme.

## Simplified Charts

### Bar Chart

```svelte
<script lang="ts">
  import * as Chart from "$lib/components/ui/chart/index.js";
  import { BarChart } from "layerchart";

  const data = [
    { month: "Jan", revenue: 4200, costs: 2800 },
    { month: "Feb", revenue: 5100, costs: 3200 },
    { month: "Mar", revenue: 4800, costs: 2900 },
  ];
</script>

<Chart.Container class="h-64">
  <BarChart {data} x="month" series={["revenue", "costs"]} legend>
    {#snippet tooltip()}
      <Chart.Tooltip />
    {/snippet}
  </BarChart>
</Chart.Container>
```

### Line Chart

```svelte
<script lang="ts">
  import * as Chart from "$lib/components/ui/chart/index.js";
  import { LineChart } from "layerchart";

  const data = [
    { date: new Date("2024-01-01"), value: 100 },
    { date: new Date("2024-02-01"), value: 150 },
    { date: new Date("2024-03-01"), value: 130 },
  ];
</script>

<Chart.Container class="h-64">
  <LineChart {data} x="date" y="value">
    {#snippet tooltip()}
      <Chart.Tooltip />
    {/snippet}
  </LineChart>
</Chart.Container>
```

### Area Chart

```svelte
<script lang="ts">
  import * as Chart from "$lib/components/ui/chart/index.js";
  import { AreaChart } from "layerchart";

  const data = [/* ... */];
</script>

<Chart.Container class="h-64">
  <AreaChart {data} x="date" series={["desktop", "mobile"]} legend>
    {#snippet tooltip()}
      <Chart.Tooltip />
    {/snippet}
  </AreaChart>
</Chart.Container>
```

### Pie Chart

```svelte
<script lang="ts">
  import * as Chart from "$lib/components/ui/chart/index.js";
  import { PieChart } from "layerchart";

  const data = [
    { name: "Chrome", value: 65 },
    { name: "Firefox", value: 15 },
    { name: "Safari", value: 12 },
    { name: "Edge", value: 8 },
  ];
</script>

<Chart.Container class="h-64">
  <PieChart {data} value="value" name="name" legend>
    {#snippet tooltip()}
      <Chart.Tooltip />
    {/snippet}
  </PieChart>
</Chart.Container>
```

## Composable Charts

For full control, compose primitives directly:

### Time Series with Gradient

```svelte
<script lang="ts">
  import { Chart, Svg, Area, Spline, Axis, Grid, Highlight } from "layerchart";
  import { LinearGradient } from "layerchart";
  import { scaleTime } from "d3-scale";
  import * as ChartUI from "$lib/components/ui/chart/index.js";

  const data = [/* { date: Date, value: number }[] */];
</script>

<ChartUI.Container class="h-80">
  <Chart
    {data}
    x="date"
    y="value"
    xScale={scaleTime()}
    padding={{ top: 20, right: 10, bottom: 30, left: 40 }}
  >
    <Svg>
      <Grid horizontal class="stroke-muted" />
      <Axis placement="bottom" format={(d) => d.toLocaleDateString('sv-SE', { month: 'short' })} />
      <Axis placement="left" />

      <LinearGradient id="area-gradient" from="hsl(var(--chart-1))" to="transparent" vertical />
      <Area class="fill-[url(#area-gradient)]" />
      <Spline class="stroke-[hsl(var(--chart-1))] stroke-2" />
      <Highlight points lines />
    </Svg>
  </Chart>
</ChartUI.Container>
```

### Chart Context Access (Svelte 5)

Access scales and dimensions via snippet:

```svelte
<Chart {data} x="date" y="value">
  {#snippet children({ context })}
    <Svg>
      <!-- context.xScale, context.yScale, context.width, context.height -->
      <Axis placement="bottom" />
    </Svg>
  {/snippet}
</Chart>
```

## Tooltips

### Tooltip Modes

Choose the mode based on chart type:

| Mode | Best For | Description |
|------|----------|-------------|
| `bisect-x` | Time series, line/area charts | Snaps to nearest x value |
| `band` | Bar charts | Snaps to bar bands |
| `quadtree-x` | Area charts with multiple series | Nearest x point |
| `quadtree` | Scatter plots | Nearest point in 2D space |

### Svelte 5 Tooltip Pattern

```svelte
<BarChart {data} x="month" y="value" tooltip={{ mode: 'band' }}>
  {#snippet tooltip()}
    <Chart.Tooltip />
  {/snippet}
</BarChart>
```

### Custom Tooltip Content

```svelte
<LineChart {data} x="date" y="value">
  {#snippet tooltip({ data: tooltipData })}
    <div class="rounded-lg border bg-background p-2 shadow-sm">
      <p class="text-sm font-medium">{tooltipData.date.toLocaleDateString()}</p>
      <p class="text-lg font-bold">${tooltipData.value.toLocaleString()}</p>
    </div>
  {/snippet}
</LineChart>
```

### Typed Tooltip Data (TypeScript)

```svelte
{#snippet tooltip({ data }: { data: { date: Date; value: number } })}
  <div class="p-2">
    <span>{data.date.toLocaleDateString()}</span>
    <span class="font-bold">{data.value}</span>
  </div>
{/snippet}
```

## Chart Types Gallery

### Cartesian
- `BarChart` / `Bars` + `BarStack` + `BarGroup`
- `LineChart` / `Spline`
- `AreaChart` / `Area`
- `ScatterChart` / `Points`
- `Threshold` (above/below reference line)
- `Calendar` (GitHub-style contribution grid)

### Radial
- `PieChart` / `Pie`
- `ArcChart` / `Arc`

### Hierarchical
- `Treemap`
- `Pack` (circle packing)
- `Partition` (sunburst)
- `Tree`
- `Sankey`

### Graph
- `ForceSimulation`
- `Dagre` (directed acyclic graph)

### Geographic
- `GeoContext` + `GeoPath` (choropleth maps)
- `GeoPoint` + `GeoSpline`
- `TileImage` (map tiles)
- Projections: Mercator, Azimuthal, etc.

### Interactions
- `Tooltip` / `TooltipContext`
- `Highlight` (hover emphasis)
- `BrushContext` (selection range)
- `TransformContext` (pan/zoom)
- `Voronoi` (nearest-point detection)

### Annotations
- `AnnotationLine`
- `AnnotationPoint`
- `AnnotationRange`

## Theming and Colors

### Using Chart CSS Variables

The `--chart-1` through `--chart-5` variables defined in `app.css` map to Tailwind classes via `@theme inline`:

```svelte
<Bars class="fill-chart-1" />
<Spline class="stroke-chart-2 stroke-2" />
<Area class="fill-chart-3/50" />
```

### Custom Color Schemes

```css
/* app.css */
:root {
  --chart-revenue: oklch(0.7 0.15 145);
  --chart-costs: oklch(0.6 0.2 25);
}

@theme inline {
  --color-chart-revenue: var(--chart-revenue);
  --color-chart-costs: var(--chart-costs);
}
```

```svelte
<BarChart {data} x="month" series={["revenue", "costs"]}>
  <!-- Series automatically use chart colors in order -->
</BarChart>
```

### Gradients

```svelte
<Svg>
  <LinearGradient id="my-gradient" from="hsl(var(--chart-1))" to="transparent" vertical />
  <Area class="fill-[url(#my-gradient)]" />
</Svg>
```

## Storybook Stories

```svelte
<!-- src/lib/components/charts/BarChartDemo.stories.svelte -->
<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import * as Chart from "$lib/components/ui/chart/index.js";
  import { BarChart } from "layerchart";

  const { Story } = defineMeta({
    title: 'Charts/BarChart',
    tags: ['autodocs'],
  });
</script>

<Story name="Basic">
  {#snippet template()}
    <Chart.Container class="h-64">
      <BarChart
        data={[
          { month: "Jan", value: 186 },
          { month: "Feb", value: 305 },
          { month: "Mar", value: 237 },
        ]}
        x="month"
        y="value"
      >
        {#snippet tooltip()}
          <Chart.Tooltip />
        {/snippet}
      </BarChart>
    </Chart.Container>
  {/snippet}
</Story>
```

> **Note:** For Storybook stories that import LayerChart, ensure `layerchart@next` resolves correctly — verify `nodeModulesDir: "auto"` in `deno.json`.

## Component Reference

### Charts (Simplified)

| Component | Props | Description |
|-----------|-------|-------------|
| `Chart` | `data`, `x`, `y`, `xScale`, `yScale`, `padding` | Root composable chart |
| `BarChart` | `data`, `x`, `series`, `legend`, `tooltip` | Pre-composed bar chart |
| `LineChart` | `data`, `x`, `y`, `series`, `legend` | Pre-composed line chart |
| `AreaChart` | `data`, `x`, `series`, `legend` | Pre-composed area chart |
| `PieChart` | `data`, `value`, `name`, `legend` | Pre-composed pie chart |
| `ScatterChart` | `data`, `x`, `y` | Pre-composed scatter plot |

### Layers

| Component | Description |
|-----------|-------------|
| `Svg` | SVG rendering layer |
| `Html` | HTML overlay layer (tooltips, labels) |
| `Canvas` | Canvas layer (high-performance rendering) |

### Common

| Component | Key Props | Description |
|-----------|-----------|-------------|
| `Axis` | `placement` (`top`, `bottom`, `left`, `right`), `format` | Axis with ticks |
| `Grid` | `horizontal`, `vertical` | Grid lines |
| `Legend` | `selected` | Chart legend |
| `Rule` | `x`, `y` | Reference line |

### Key Resources

- **All examples**: https://next.layerchart.com/docs/examples
- **Component API**: https://next.layerchart.com/docs/components/Chart
- **Styling guide**: https://next.layerchart.com/docs/guides/styles
- **LLM guide**: https://next.layerchart.com/docs/guides/LLMs
- **shadcn-svelte charts gallery**: https://www.shadcn-svelte.com/charts
