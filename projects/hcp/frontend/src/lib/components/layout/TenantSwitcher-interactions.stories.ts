/**
 * CSF3 interaction tests for TenantSwitcher.
 *
 * Tests dropdown open, session display, and badge states.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import TestHarness from "./TenantSwitcher-test-harness.svelte";

const meta = {
  title: "Tests/TenantSwitcher Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: TestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<typeof TestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Open the dropdown and verify all sessions are listed.
 */
export const ShowsSessions: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // Click the trigger button to open the dropdown
    const trigger = canvas.getByRole("button", { name: /dev-ai/i });
    await userEvent.click(trigger);

    // Dropdown content is portaled to document.body
    const body = within(canvasElement.ownerDocument.body);

    // Verify sessions are listed
    await expect(body.getByText("Tenant Sessions")).toBeInTheDocument();
    await expect(body.getByText("prod-ai")).toBeInTheDocument();
    await expect(body.getByText("staging")).toBeInTheDocument();
  },
};

/**
 * Verify badge states (Active, Switch, Expired).
 */
export const ShowsBadgeStates: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    const trigger = canvas.getByRole("button", { name: /dev-ai/i });
    await userEvent.click(trigger, { pointerEventsCheck: 0 });

    const body = within(canvasElement.ownerDocument.body);

    await expect(body.getByText("Active")).toBeInTheDocument();
    await expect(body.getByText("Switch")).toBeInTheDocument();
    await expect(body.getByText("Expired")).toBeInTheDocument();
  },
};

/**
 * Verify "Add another tenant" option is present.
 */
export const ShowsAddTenant: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    const trigger = canvas.getByRole("button", { name: /dev-ai/i });
    await userEvent.click(trigger, { pointerEventsCheck: 0 });

    const body = within(canvasElement.ownerDocument.body);

    await expect(
      body.getByText("Add another tenant"),
    ).toBeInTheDocument();
  },
};
