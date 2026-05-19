/**
 * CSF3 interaction tests for IpListEditor.
 *
 * Tests adding IPs (type + Enter and button), removing IPs, and duplicate prevention.
 *
 * Run `make storybook` and open the "Interactions" panel to see results.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import IpListEditorTestHarness from "./ip-list-editor-test-harness.svelte";

const meta = {
  title: "Tests/IpListEditor Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: IpListEditorTestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<IpListEditorTestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Verify initial addresses render correctly.
 */
export const RendersInitialAddresses: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    await expect(canvas.getByText("10.0.0.0/8")).toBeInTheDocument();
    await expect(canvas.getByText("192.168.1.0/24")).toBeInTheDocument();
    await expect(canvas.getByTestId("ip-count")).toHaveTextContent(
      "2 address(es)",
    );
  },
};

/**
 * Add an IP address via Enter key.
 */
export const AddIpViaEnter: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const input = canvas.getByPlaceholderText(
      "IP address or CIDR (e.g. 10.0.0.0/8)",
    );

    await userEvent.type(input, "172.16.0.0/12{Enter}");

    await expect(canvas.getByText("172.16.0.0/12")).toBeInTheDocument();
    await expect(canvas.getByTestId("ip-count")).toHaveTextContent(
      "3 address(es)",
    );
  },
};

/**
 * Add an IP address via the + button.
 */
export const AddIpViaButton: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const input = canvas.getByPlaceholderText(
      "IP address or CIDR (e.g. 10.0.0.0/8)",
    );
    // The add button has a Plus icon, find it by role
    const buttons = canvas.getAllByRole("button");
    // The + button is the one next to the input (not inside badges)
    const addButton = buttons[0];

    await userEvent.type(input, "10.10.10.10");
    await userEvent.click(addButton);

    await expect(canvas.getByText("10.10.10.10")).toBeInTheDocument();
    await expect(canvas.getByTestId("ip-count")).toHaveTextContent(
      "3 address(es)",
    );
  },
};

/**
 * Duplicate IPs should not be added.
 */
export const PreventsDuplicates: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const input = canvas.getByPlaceholderText(
      "IP address or CIDR (e.g. 10.0.0.0/8)",
    );

    // Try adding an existing address
    await userEvent.type(input, "10.0.0.0/8{Enter}");

    // Count should remain 2
    await expect(canvas.getByTestId("ip-count")).toHaveTextContent(
      "2 address(es)",
    );
  },
};

/**
 * Remove an IP by clicking the X button on its badge.
 */
export const RemoveIp: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // Find the badge <span> containing "10.0.0.0/8" and its X button
    const badge = canvas.getByText("10.0.0.0/8").closest(
      "[data-slot='badge']",
    )!;
    const removeButton = within(badge as HTMLElement).getByRole("button");
    await userEvent.click(removeButton);

    await expect(canvas.queryByText("10.0.0.0/8")).not.toBeInTheDocument();
    await expect(canvas.getByTestId("ip-count")).toHaveTextContent(
      "1 address(es)",
    );
  },
};
