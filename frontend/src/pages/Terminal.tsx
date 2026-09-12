"use client";

import { useEffect, useState } from "react";
import { useTerminal } from "@/store/terminal";
import TopBar from "@/components/terminal/TopBar";
import Sidebar from "@/components/terminal/Sidebar";
import Workspace from "@/components/terminal/Workspace";
import CommandPalette from "@/components/terminal/CommandPalette";
import { EventsWidget } from "@/components/terminal/widgets/EventsWidget";
import { SignalsWidget } from "@/components/terminal/widgets/SignalsWidget";
import { SchedulerWidget } from "@/components/terminal/widgets/SchedulerWidget";
import { PaperPnlWidget } from "@/components/terminal/widgets/PaperPnlWidget";
import { SafetyWidget } from "@/components/terminal/widgets/SafetyWidget";

const BOTTOM_TABS = [
  { id: "trades" as const, label: "TRADES", icon: "📋" },
  { id: "events" as const, label: "EVENTS", icon: "📜" },
  { id: "scheduler" as const, label: "SCHEDULER", icon: "⏱" },
  { id: "signals" as const, label: "SIGNALS", icon: "🎯" },
  { id: "pnl" as const, label: "P&L", icon: "💰" },
  { id: "safety" as const, label: "SAFETY", icon: "🛡" },
] as const;

function BottomTerminal() {
  const bottomTab = useTerminal((s) => s.bottomTab);
  const setBottomTab = useTerminal((s) => s.setBottomTab);

  return (
    <div className="h-[200px] bg-[var(--bg-elev)] border-t border-[var(--border)] flex flex-col shrink-0">
      <div className="flex items-center gap-1 px-2 py-1 border-b border-[var(--border-soft)] bg-[var(--bg-elev-2)] shrink-0">
        <span className="font-mono text-[10px] text-[var(--text-dim)] uppercase tracking-wider">TERMINAL</span>
        <span className="w-px h-4 bg-[var(--border)] mx-1" />
        {BOTTOM_TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setBottomTab(tab.id)}
            className={`flex items-center gap-1 px-2 py-1 text-[10px] rounded transition-colors ${
              bottomTab === tab.id
                ? "bg-[var(--accent-soft)] text-[var(--amber)]"
                : "dim hover:text-[var(--text)] hover:bg-[#1a1a1a]"
            }`}
          >
            <span>{tab.icon}</span>
            <span>{tab.label}</span>
          </button>
        ))}
        <span className="flex-1" />
        <span className="dim text-[10px]">Bottom panel — drag to resize</span>
      </div>
      <div className="flex-1 overflow-auto">
        {bottomTab === "trades" && (
          <div className="p-2">
            <div className="dim text-[9px] uppercase tracking-wider mb-2">PAPER TRADES (Recent)</div>
            <div className="text-center dim p-4">Trade history widget — add to workspace for full view</div>
          </div>
        )}
        {bottomTab === "events" && <EventsWidget widget={{ id: "events-bottom", type: "events", x: 0, y: 0, w: 12, h: 6 }} />}
        {bottomTab === "scheduler" && <SchedulerWidget widget={{ id: "scheduler-bottom", type: "scheduler", x: 0, y: 0, w: 12, h: 6 }} />}
        {bottomTab === "signals" && <SignalsWidget widget={{ id: "signals-bottom", type: "signals", x: 0, y: 0, w: 12, h: 6 }} />}
        {bottomTab === "pnl" && <PaperPnlWidget widget={{ id: "pnl-bottom", type: "paper-pnl", x: 0, y: 0, w: 12, h: 6 }} />}
        {bottomTab === "safety" && <SafetyWidget widget={{ id: "safety-bottom", type: "safety", x: 0, y: 0, w: 12, h: 6 }} />}
      </div>
    </div>
  );
}

export default function TerminalPage() {
  const commandOpen = useTerminal((s) => s.commandOpen);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) {
    return (
      <div className="h-screen w-screen bg-[var(--bg)] flex items-center justify-center">
        <div className="text-center">
          <div className="text-[var(--amber)] font-bold text-[24px] tracking-widest mb-4">FINOVA TERMINAL</div>
          <div className="spinner mx-auto" style={{ width: 32, height: 32, borderWidth: 3 }} />
          <div className="dim mt-4 text-[12px]">Initializing workspace…</div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen w-screen bg-[var(--bg)] flex flex-col overflow-hidden">
      <TopBar />
      <div className="flex-1 flex overflow-hidden">
        <Sidebar />
        <div className="flex-1 flex flex-col overflow-hidden min-w-0">
          <Workspace />
          <BottomTerminal />
        </div>
      </div>
      <CommandPalette />
    </div>
  );
}