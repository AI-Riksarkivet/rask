/**
 * CSF3 interaction tests for BulkDeleteDialog.
 *
 * Tests confirm flow and force delete checkbox.
 *
 * Note: Dialogs render to a portal outside #storybook-root, so we query
 * document.body instead of canvasElement for portal content.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import TestHarness from "./bulk-delete-dialog-test-harness.svelte";

const meta = {
  title: "Tests/BulkDeleteDialog Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: TestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<typeof TestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Verify the dialog renders with correct plural content.
 */
export const RendersContent: Story = {
  play: async ({ canvasElement }) => {
    // Dialog content is portaled to document.body, outside canvasElement
    const body = within(canvasElement.ownerDocument.body);

    // Title and confirm button both contain "Delete 5 namespaces"
    const matches = body.getAllByText("Delete 5 namespaces");
    await expect(matches.length).toBeGreaterThanOrEqual(1);
    await expect(body.getByText("Cancel")).toBeInTheDocument();
  },
};

/**
 * Click confirm and verify count increments.
 */
export const ConfirmBulkDelete: Story = {
  play: async ({ canvasElement }) => {
    const body = within(canvasElement.ownerDocument.body);
    const canvas = within(canvasElement);

    const deleteBtn = body.getByRole("button", {
      name: "Delete 5 namespaces",
    });
    await userEvent.click(deleteBtn);

    await new Promise((r) => setTimeout(r, 600));

    const counter = canvas.getByTestId("confirm-count");
    await expect(counter).toHaveTextContent("Confirmed: 1");
  },
};

/**
 * Toggle force delete checkbox.
 */
export const ForceDeleteCheckbox: Story = {
  play: async ({ canvasElement }) => {
    const body = within(canvasElement.ownerDocument.body);

    await expect(body.getByText(/Force delete/)).toBeInTheDocument();

    const doc = canvasElement.ownerDocument;
    const checkbox = doc.querySelector(
      'button[role="checkbox"]',
    ) as HTMLElement;
    await expect(checkbox).toBeInTheDocument();
    await userEvent.click(checkbox);

    await expect(checkbox).toHaveAttribute("data-state", "checked");
  },
};
