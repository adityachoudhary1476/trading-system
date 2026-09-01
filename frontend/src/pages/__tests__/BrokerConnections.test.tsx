import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/upstox", () => ({
  fetchConnectionStatus: vi.fn(),
  startUpstoxOAuth: vi.fn(),
  disconnectUpstox: vi.fn(),
}));

import { useAuth } from "@/contexts/AuthContext";
import { fetchConnectionStatus, startUpstoxOAuth, disconnectUpstox } from "@/lib/upstox";
import { BrokerConnectionsPage } from "@/pages/BrokerConnections";

const mockUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>;
const mockFetchStatus = fetchConnectionStatus as unknown as ReturnType<typeof vi.fn>;
const mockStartOAuth = startUpstoxOAuth as unknown as ReturnType<typeof vi.fn>;
const mockDisconnect = disconnectUpstox as unknown as ReturnType<typeof vi.fn>;

function renderPage(path = "/broker") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/broker" element={<BrokerConnectionsPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("BrokerConnectionsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows sign-in form when unauthenticated", () => {
    mockUseAuth.mockReturnValue({ user: null, loading: false, signIn: vi.fn(), signOut: vi.fn() });
    renderPage();
    expect(screen.getByPlaceholderText("Email")).toBeTruthy();
    expect(screen.getByPlaceholderText("Password")).toBeTruthy();
  });

  it("does not render any access token or secret", () => {
    mockUseAuth.mockReturnValue({
      user: { id: "u1", email: "a@b.co" },
      loading: false,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mockFetchStatus.mockResolvedValue({
      connected: true,
      provider: "upstox",
      obtained_at: "2026-01-01T00:00:00Z",
    });
    renderPage();
    expect(screen.queryByText(/eyJ/i)).toBeNull();
    expect(screen.queryByText(/access_token/i)).toBeNull();
    expect(screen.queryByText(/secret/i)).toBeNull();
  });

  it("shows Connect Upstox button when not connected", async () => {
    mockUseAuth.mockReturnValue({
      user: { id: "u1", email: "a@b.co" },
      loading: false,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mockFetchStatus.mockResolvedValue({ connected: false });
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Connect Upstox")).toBeTruthy();
    });
  });

  it("shows Connected status when connected", async () => {
    mockUseAuth.mockReturnValue({
      user: { id: "u1", email: "a@b.co" },
      loading: false,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mockFetchStatus.mockResolvedValue({
      connected: true,
      provider: "upstox",
      obtained_at: "2026-01-01T00:00:00Z",
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Connected")).toBeTruthy();
    });
  });

  it("shows Disconnect button when connected", async () => {
    mockUseAuth.mockReturnValue({
      user: { id: "u1", email: "a@b.co" },
      loading: false,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mockFetchStatus.mockResolvedValue({
      connected: true,
      provider: "upstox",
      obtained_at: "2026-01-01T00:00:00Z",
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Disconnect")).toBeTruthy();
    });
  });

  it("calls disconnectUpstox when Disconnect is clicked", async () => {
    mockUseAuth.mockReturnValue({
      user: { id: "u1", email: "a@b.co" },
      loading: false,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mockFetchStatus.mockResolvedValue({
      connected: true,
      provider: "upstox",
      obtained_at: "2026-01-01T00:00:00Z",
    });
    mockDisconnect.mockResolvedValue({ disconnected: true });
    renderPage();
    await waitFor(() => screen.getByText("Disconnect"));
    fireEvent.click(screen.getByText("Disconnect"));
    await waitFor(() => {
      expect(mockDisconnect).toHaveBeenCalledTimes(1);
    });
  });

  it("calls startUpstoxOAuth when Connect Upstox is clicked", async () => {
    mockUseAuth.mockReturnValue({
      user: { id: "u1", email: "a@b.co" },
      loading: false,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mockFetchStatus.mockResolvedValue({ connected: false });
    mockStartOAuth.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => screen.getByText("Connect Upstox"));
    fireEvent.click(screen.getByText("Connect Upstox"));
    await waitFor(() => {
      expect(mockStartOAuth).toHaveBeenCalledTimes(1);
    });
  });

  it("shows success feedback when connected=success", () => {
    mockUseAuth.mockReturnValue({
      user: { id: "u1", email: "a@b.co" },
      loading: false,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mockFetchStatus.mockResolvedValue({ connected: false });
    renderPage("/broker?connected=success");
    expect(screen.getByText(/connected successfully/i)).toBeTruthy();
  });

  it("does not show Connected when status fetch fails", async () => {
    mockUseAuth.mockReturnValue({
      user: { id: "u1", email: "a@b.co" },
      loading: false,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mockFetchStatus.mockRejectedValue(new Error("network error"));
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Connect Upstox")).toBeTruthy();
    });
    expect(screen.queryByText("Connected")).toBeNull();
  });
});
