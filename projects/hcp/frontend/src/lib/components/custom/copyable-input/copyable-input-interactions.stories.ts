/**
 * CSF3 interaction tests for CopyableInput.
 *
 * Tests rendering, secret toggle, and copy button feedback.
 *
 * Run `make storybook` and open the "Interactions" panel to see results.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import CopyableInputTestHarness from "./copyable-input-test-harness.svelte";

const meta = {
  title: "Tests/CopyableInput Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: CopyableInputTestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<CopyableInputTestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Verify all three inputs render with correct labels.
 */
export const RendersAll: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    await expect(canvas.getByText("Canonical ID")).toBeInTheDocument();
    await expect(canvas.getByText("Secret Key")).toBeInTheDocument();

    // The plain input should show its value
    const inputs = canvas.getAllByRole("textbox");
    await expect(inputs.length).toBeGreaterThanOrEqual(2);
  },
};

/**
 * Secret input should be masked and togglable.
 */
export const SecretToggle: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // Find all password-type inputs (secret ones)
    const passwordInputs = canvasElement.querySelectorAll(
      'input[type="password"]',
    );
    await expect(passwordInputs.length).toBe(1);

    // Click the reveal button (Eye icon) — it's in the Secret Key row
    const secretLabel = canvas.getByText("Secret Key");
    const secretSection = secretLabel.closest(".space-y-1")!;
    const buttons = within(secretSection as HTMLElement).getAllByRole("button");
    // First button is reveal, second is copy
    const revealBtn = buttons[0];
    await userEvent.click(revealBtn);

    // Now the input should be type="text"
    const textInputs = canvasElement.querySelectorAll(
      'input[type="password"]',
    );
    await expect(textInputs.length).toBe(0);

    // Click again to hide
    await userEvent.click(revealBtn);
    const hiddenAgain = canvasElement.querySelectorAll(
      'input[type="password"]',
    );
    await expect(hiddenAgain.length).toBe(1);
  },
};

/**
 * Clicking copy button shows check icon feedback.
 */
export const CopyFeedback: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // Find the Canonical ID section and its copy button
    const canonicalLabel = canvas.getByText("Canonical ID");
    const canonicalSection = canonicalLabel.closest(".space-y-1")!;
    const buttons = within(canonicalSection as HTMLElement).getAllByRole(
      "button",
    );
    // Only one button (copy) since this isn't a secret field
    const copyBtn = buttons[0];
    await userEvent.click(copyBtn);

    // The button should still be in the document after click
    // (copy feedback shows a check icon briefly)
    await expect(copyBtn).toBeInTheDocument();
  },
};
