import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { DeploymentPicker } from "@/components/paper/paperShared";
import { paperApi } from "@/lib/paperApi";

vi.mock("@/lib/paperApi", () => ({
  paperApi: {
    listDeployments: vi.fn(),
  },
}));

const mockDeployments = {
  deployments: [
    {
      deployment_id: "dep-1",
      strategy_id: "strat-1",
      strategy_spec_hash: "hash-1",
      symbol: "NSE:SBIN",
      timeframe: "1d",
      execution_mode: "paper",
      dataset_id: "ds-1",
      status: "active",
      created_at: "2024-01-01T00:00:00Z",
      activated_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
      notes: "",
      schema_version: 1,
    },
  ],
  count: 1,
  schema_version: 1,
};

describe("DeploymentPicker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows loading state while fetching", () => {
    vi.mocked(paperApi.listDeployments).mockImplementation(
      () => new Promise(() => {})
    );
    render(<DeploymentPicker value="" onChange={() => {}} />);
    expect(screen.getByText("Loading deployments…")).toBeDefined();
  });

  it("surfaces network errors instead of silently going empty", async () => {
    vi.mocked(paperApi.listDeployments).mockResolvedValue({
      ok: false,
      error: { code: "network_error", message: "Network request failed" },
      status: 0,
    } as any);
    render(<DeploymentPicker value="" onChange={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText("Unable to load deployments")).toBeDefined();
    });
    expect(screen.getByText("Network request failed")).toBeDefined();
    expect(screen.getByRole("button", { name: "Retry" })).toBeDefined();
  });

  it("offers a create action on the empty state", async () => {
    vi.mocked(paperApi.listDeployments).mockResolvedValue({
      ok: true,
      data: { deployments: [], count: 0, schema_version: 1 },
    } as any);
    const onCreate = vi.fn();
    render(<DeploymentPicker value="" onChange={() => {}} onCreateDeployment={onCreate} />);
    await waitFor(() => {
      expect(screen.getByText("No paper deployments yet")).toBeDefined();
    });
    const createBtn = screen.getByRole("button", { name: "Create deployment" });
    fireEvent.click(createBtn);
    expect(onCreate).toHaveBeenCalled();
  });

  it("renders a select with options once deployments load", async () => {
    vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: true, data: mockDeployments } as any);
    render(<DeploymentPicker value="" onChange={() => {}} />);
    await waitFor(() => {
      expect(screen.getByLabelText("Select deployment")).toBeDefined();
    });
    expect(screen.getByText(/dep-1/)).toBeDefined();
  });

  it("refetches when refreshKey changes", async () => {
    vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: true, data: mockDeployments } as any);
    const { rerender } = render(<DeploymentPicker value="" onChange={() => {}} refreshKey={0} />);
    await waitFor(() => {
      expect(screen.getByLabelText("Select deployment")).toBeDefined();
    });
    expect(paperApi.listDeployments).toHaveBeenCalledTimes(1);
    rerender(<DeploymentPicker value="" onChange={() => {}} refreshKey={1} />);
    await waitFor(() => {
      expect(paperApi.listDeployments).toHaveBeenCalledTimes(2);
    });
  });

  it("reports selection via onChange", async () => {
    vi.mocked(paperApi.listDeployments).mockResolvedValue({ ok: true, data: mockDeployments } as any);
    const onChange = vi.fn();
    render(<DeploymentPicker value="" onChange={onChange} />);
    await waitFor(() => {
      expect(screen.getByLabelText("Select deployment")).toBeDefined();
    });
    fireEvent.change(screen.getByLabelText("Select deployment"), { target: { value: "dep-1" } });
    expect(onChange).toHaveBeenCalledWith("dep-1");
  });
});
