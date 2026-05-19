/**
 * CSF3 interaction tests for FormDialog.
 *
 * Tests form submission, validation errors, and cancel behavior.
 * Note: Bits UI Dialog renders via a portal into document.body,
 * so dialog content must be queried from document.body, not canvasElement.
 * Uses findBy* (async) queries since the portal content renders asynchronously.
 *
 * Run `make storybook` and open the "Interactions" panel to see results.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import FormDialogTestHarness from "./form-dialog-test-harness.svelte";

const meta = {
  title: "Tests/FormDialog Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: FormDialogTestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<FormDialogTestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Query helper — dialog content is portaled to document.body */
function getPage() {
  return within(document.body);
}

/**
 * Verify the dialog renders with title and description.
 */
export const RendersDialog: Story = {
  play: async () => {
    const page = getPage();

    // Use findBy* to wait for the portaled dialog content
    await expect(
      await page.findByText("Create Namespace"),
    ).toBeInTheDocument();
    await expect(
      await page.findByText("Create a new namespace."),
    ).toBeInTheDocument();
    await expect(
      await page.findByPlaceholderText("my-namespace"),
    ).toBeInTheDocument();
    await expect(
      await page.findByRole("button", { name: "Create" }),
    ).toBeInTheDocument();
    await expect(
      await page.findByRole("button", { name: "Cancel" }),
    ).toBeInTheDocument();
  },
};

/**
 * Submit with empty name shows validation error.
 */
export const ValidationError: Story = {
  play: async () => {
    const page = getPage();
    const submitBtn = await page.findByRole("button", { name: "Create" });

    await userEvent.click(submitBtn);

    await expect(
      await page.findByText("Name is required."),
    ).toBeInTheDocument();
  },
};

/**
 * Submit with duplicate name shows conflict error.
 */
export const ConflictError: Story = {
  play: async () => {
    const page = getPage();
    const input = await page.findByPlaceholderText("my-namespace");
    const submitBtn = await page.findByRole("button", { name: "Create" });

    await userEvent.type(input, "existing");
    await userEvent.click(submitBtn);

    await expect(
      await page.findByText("Namespace 'existing' already exists."),
    ).toBeInTheDocument();
  },
};

/**
 * Successful submission shows success message.
 */
export const SuccessfulSubmit: Story = {
  play: async ({ canvasElement }) => {
    const page = getPage();
    const canvas = within(canvasElement);
    const input = await page.findByPlaceholderText("my-namespace");
    const submitBtn = await page.findByRole("button", { name: "Create" });

    await userEvent.type(input, "new-namespace");
    await userEvent.click(submitBtn);

    // Success message renders in the harness (inside canvasElement), not the dialog
    await expect(
      await canvas.findByTestId("success-msg"),
    ).toHaveTextContent("Created namespace: new-namespace");
  },
};
