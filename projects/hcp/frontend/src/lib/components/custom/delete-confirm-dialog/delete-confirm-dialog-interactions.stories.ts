/**
 * CSF3 interaction tests for DeleteConfirmDialog.
 *
 * Tests confirm and cancel flows.
 *
 * Note: Dialogs render to a portal outside #storybook-root, so we query
 * document.body instead of canvasElement for portal content.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import TestHarness from "./delete-confirm-dialog-test-harness.svelte";

const meta = {
  title: "Tests/DeleteConfirmDialog Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: TestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<typeof TestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Verify the dialog renders with correct content.
 */
export const RendersContent: Story = {
  play: async ({ canvasElement }) => {
    // Dialog content is portaled to document.body, outside canvasElement
    const body = within(canvasElement.ownerDocument.body);

    await expect(body.getByText("Delete namespace")).toBeInTheDocument();
    await expect(body.getByText(/production-data/)).toBeInTheDocument();
    await expect(body.getByText("Cancel")).toBeInTheDocument();
    await expect(
      body.getByRole("button", { name: "Delete" }),
    ).toBeInTheDocument();
  },
};

/**
 * Click Delete and verify confirm count increments.
 */
export const ConfirmDelete: Story = {
  play: async ({ canvasElement }) => {
    const body = within(canvasElement.ownerDocument.body);
    const canvas = within(canvasElement);

    const deleteBtn = body.getByRole("button", { name: "Delete" });
    await userEvent.click(deleteBtn);

    // Wait for loading to complete and dialog to close
    await new Promise((r) => setTimeout(r, 600));

    const counter = canvas.getByTestId("confirm-count");
    await expect(counter).toHaveTextContent("Confirmed: 1");
  },
};

/**
 * Verify force delete checkbox is present and toggleable.
 */
export const ForceDeleteCheckbox: Story = {
  play: async ({ canvasElement }) => {
    const body = within(canvasElement.ownerDocument.body);

    await expect(body.getByText(/Force delete/)).toBeInTheDocument();

    // Find and click the checkbox (portaled to document body)
    const doc = canvasElement.ownerDocument;
    const checkbox = doc.querySelector(
      'button[role="checkbox"]',
    ) as HTMLElement;
    await expect(checkbox).toBeInTheDocument();
    await userEvent.click(checkbox);

    // Checkbox should now be checked
    await expect(checkbox).toHaveAttribute("data-state", "checked");
  },
};
