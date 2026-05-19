/**
 * CSF3 interaction tests for DataTable.
 *
 * These use play() functions with @testing-library/user-event and
 * storybook/test assertions to automate UI interactions in the browser.
 *
 * Note: Bits UI Checkbox uses pointer-events:none internally, so checkbox
 * tests use userEvent.setup({ pointerEventsCheck: 0 }).
 *
 * Run `make storybook` and open the "Interactions" panel to see results.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import DataTableTestHarness from "./data-table-test-harness.svelte";

const meta = {
  title: "Tests/DataTable Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: DataTableTestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<DataTableTestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Verify the table renders all rows.
 */
export const RendersAllRows: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // 10 mock namespaces should produce 10 data rows
    const rows = canvas.getAllByRole("row");
    // 1 header row + 10 data rows = 11
    await expect(rows.length).toBe(11);

    // Check specific namespaces are visible
    await expect(canvas.getByText("production-data")).toBeInTheDocument();
    await expect(canvas.getByText("ml-training")).toBeInTheDocument();
    await expect(canvas.getByText("shared-assets")).toBeInTheDocument();
  },
};

/**
 * Type in the search box and verify filtering works.
 */
export const SearchFiltering: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const searchInput = canvas.getByPlaceholderText("Search namespaces...");

    // Type "prod" — should filter to 1 result
    await userEvent.clear(searchInput);
    await userEvent.type(searchInput, "prod");

    await expect(canvas.getByText("production-data")).toBeInTheDocument();
    await expect(canvas.queryByText("staging-env")).not.toBeInTheDocument();
    await expect(canvas.queryByText("dev-sandbox")).not.toBeInTheDocument();

    // Clear and type "backup"
    await userEvent.clear(searchInput);
    await userEvent.type(searchInput, "backup");

    await expect(canvas.getByText("backup-vault")).toBeInTheDocument();
    await expect(
      canvas.queryByText("production-data"),
    ).not.toBeInTheDocument();

    // Clear search — all rows should return
    await userEvent.clear(searchInput);
    const rows = canvas.getAllByRole("row");
    await expect(rows.length).toBe(11);
  },
};

/**
 * Verify "no results" message when search matches nothing.
 */
export const SearchNoResults: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const searchInput = canvas.getByPlaceholderText("Search namespaces...");

    await userEvent.clear(searchInput);
    await userEvent.type(searchInput, "zzz-nonexistent");

    await expect(
      canvas.getByText(/No results matching/),
    ).toBeInTheDocument();

    // Clean up
    await userEvent.clear(searchInput);
  },
};

/**
 * Select individual rows via checkboxes, verify selection bar appears.
 */
export const RowSelection: Story = {
  play: async ({ canvasElement }) => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const canvas = within(canvasElement);
    const checkboxes = canvas.getAllByRole("checkbox");

    // First checkbox is "select all", rest are row checkboxes
    // Click first row checkbox (index 1)
    await user.click(checkboxes[1]);

    // Selection bar should appear — use exact text to avoid matching footer
    await expect(canvas.getByText("1 selected")).toBeInTheDocument();

    // Click second row checkbox
    await user.click(checkboxes[2]);
    await expect(canvas.getByText("2 selected")).toBeInTheDocument();

    // Click "Deselect All"
    const deselectBtn = canvas.getByText("Deselect All");
    await user.click(deselectBtn);

    // Selection bar should disappear
    await expect(canvas.queryByText("Deselect All")).not.toBeInTheDocument();
  },
};

/**
 * Click "select all" checkbox, verify all rows selected.
 */
export const SelectAll: Story = {
  play: async ({ canvasElement }) => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const canvas = within(canvasElement);
    const checkboxes = canvas.getAllByRole("checkbox");

    // First checkbox is "select all"
    await user.click(checkboxes[0]);

    // Should show "10 selected" in the bar
    await expect(canvas.getByText("10 selected")).toBeInTheDocument();

    // Deselect all
    await user.click(checkboxes[0]);
    await expect(canvas.queryByText("Deselect All")).not.toBeInTheDocument();
  },
};

/**
 * Verify sortable column headers work (click Name header to sort).
 */
export const ColumnSorting: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // Find the "Name" sort button
    const nameHeader = canvas.getByRole("button", { name: /Name/ });
    await userEvent.click(nameHeader);

    // After ascending sort, first data row should be alphabetically first
    const rows = canvas.getAllByRole("row");
    // Row 0 is header, row 1 is first data row
    const firstDataRow = rows[1];
    await expect(
      within(firstDataRow).getByText("analytics-warehouse"),
    ).toBeInTheDocument();

    // Click again for descending
    await userEvent.click(nameHeader);
    const rowsDesc = canvas.getAllByRole("row");
    const firstDataRowDesc = rowsDesc[1];
    await expect(
      within(firstDataRowDesc).getByText("staging-env"),
    ).toBeInTheDocument();

    // Click again to clear sort
    await userEvent.click(nameHeader);
  },
};

/**
 * Verify bulk delete button appears when rows are selected.
 */
export const BulkActionBar: Story = {
  play: async ({ canvasElement }) => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const canvas = within(canvasElement);
    const checkboxes = canvas.getAllByRole("checkbox");

    // No bulk bar initially
    await expect(
      canvas.queryByText("Delete Selected"),
    ).not.toBeInTheDocument();

    // Select 3 rows
    await user.click(checkboxes[1]);
    await user.click(checkboxes[2]);
    await user.click(checkboxes[3]);

    // Bulk bar should show with correct count and all action buttons
    await expect(canvas.getByText("3 selected")).toBeInTheDocument();
    await expect(canvas.getByText("Delete Selected")).toBeInTheDocument();
    await expect(canvas.getByText("Grant Access")).toBeInTheDocument();
    await expect(canvas.getByText("Deselect All")).toBeInTheDocument();

    // Clean up
    const deselectBtn = canvas.getByText("Deselect All");
    await user.click(deselectBtn);
  },
};
