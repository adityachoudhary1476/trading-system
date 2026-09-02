import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AppProvider } from "@/store/AppContext";
import { PaperResearch } from "@/pages/paper/PaperResearch";
import { STRATEGY_LIBRARY } from "@/data/strategyLibrary";

function renderResearch() {
  return render(
    <AppProvider>
      <MemoryRouter initialEntries={["/paper/research"]}>
        <Routes>
          <Route path="/paper/research" element={<PaperResearch />} />
        </Routes>
      </MemoryRouter>
    </AppProvider>,
  );
}

describe("PaperResearch", () => {
  it("renders the curated library (>= 10 entries)", () => {
    expect(STRATEGY_LIBRARY.length).toBeGreaterThanOrEqual(10);
    renderResearch();
    expect(screen.getByText(/Strategy Research Library/i)).toBeDefined();
  });

  it("shows the page subtitle and disclaimer", () => {
    renderResearch();
    expect(screen.getByText(/Research view only/i)).toBeDefined();
  });

  it("renders filter controls", () => {
    renderResearch();
    expect(screen.getByPlaceholderText(/name, claim, source, tag/i)).toBeDefined();
  });

  it("filters by search term", () => {
    renderResearch();
    const search = screen.getByPlaceholderText(/name, claim, source, tag/i);
    fireEvent.change(search, { target: { value: "Bollinger" } });
    // The candidate list should now contain only Bollinger-matching entries.
    const matching = STRATEGY_LIBRARY.filter(
      (c) =>
        c.name.toLowerCase().includes("bollinger") ||
        c.claim.toLowerCase().includes("bollinger") ||
        c.source.toLowerCase().includes("bollinger") ||
        c.tags.some((t) => t.toLowerCase().includes("bollinger")),
    );
    // Every matching name should be present (>= 1 occurrence).
    for (const c of matching) {
      expect(screen.queryAllByText(c.name).length).toBeGreaterThan(0);
    }
    // Non-matching entries should be absent.
    const nonMatching = STRATEGY_LIBRARY.filter(
      (c) =>
        !c.name.toLowerCase().includes("bollinger") &&
        !c.claim.toLowerCase().includes("bollinger") &&
        !c.source.toLowerCase().includes("bollinger") &&
        !c.tags.some((t) => t.toLowerCase().includes("bollinger")),
    );
    for (const c of nonMatching) {
      expect(screen.queryByText(c.name)).toBeNull();
    }
  });

  it("selects a candidate and shows its detail", () => {
    renderResearch();
    const buttons = screen.getAllByRole("button");
    const candidateBtn = buttons.find(
      (b) => b.className && b.className.includes("paper-research-candidate-btn"),
    );
    expect(candidateBtn).toBeDefined();
    fireEvent.click(candidateBtn!);
    // The detail panel should now show a "Claim" label.
    expect(screen.getAllByText(/^Claim$/i).length).toBeGreaterThan(0);
  });

  it("every entry has non-empty provenance", () => {
    for (const c of STRATEGY_LIBRARY) {
      expect(c.source.length).toBeGreaterThan(0);
      expect(c.evidence_quality.length).toBeGreaterThan(0);
      expect(c.claim.length).toBeGreaterThan(0);
    }
  });

  it("evidence quality uses the documented vocabulary", () => {
    const valid = new Set([
      "peer_reviewed",
      "reputable_practitioner",
      "blog_or_marketing",
      "unknown",
    ]);
    for (const c of STRATEGY_LIBRARY) {
      expect(valid.has(c.evidence_quality)).toBe(true);
    }
  });

  it("categories use the documented vocabulary", () => {
    const valid = new Set([
      "trend_following",
      "momentum",
      "mean_reversion",
      "breakout",
      "volatility",
      "market_regime",
      "multi_factor",
      "price_volume",
    ]);
    for (const c of STRATEGY_LIBRARY) {
      expect(valid.has(c.category)).toBe(true);
    }
  });

  it("no duplicate candidate ids", () => {
    const ids = STRATEGY_LIBRARY.map((c) => c.candidate_id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
