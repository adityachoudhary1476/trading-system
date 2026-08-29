import type { ReactNode } from "react";

export function Panel({
  title,
  actions,
  toolbar,
  children,
  className,
  subtle,
}: {
  title?: string;
  actions?: ReactNode;
  toolbar?: ReactNode;
  children: ReactNode;
  className?: string;
  subtle?: boolean;
}) {
  return (
    <section className={`panel${subtle ? " panel-subtle" : ""}${className ? ` ${className}` : ""}`}>
      {title && (
        <div className="panel-head">
          <span className="panel-title">{title}</span>
          {actions}
        </div>
      )}
      {toolbar && <div className="panel-toolbar">{toolbar}</div>}
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: "pos" | "neg" | "muted" }) {
  return (
    <div className="stat">
      <span className="label">{label}</span>
      <span className={`value${tone ? ` ${tone}` : ""}`}>{value}</span>
    </div>
  );
}

export function Badge({ kind, children }: { kind: string; children?: ReactNode }) {
  return (
    <span className={`badge ${kind}`}>{children ?? kind.replace("_", " ")}</span>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty">
      <div style={{ fontSize: 22, opacity: 0.5 }}>○</div>
      <div style={{ fontWeight: 600, color: "var(--text-dim)" }}>{title}</div>
      {hint && <div style={{ fontSize: 12 }}>{hint}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="empty">
      <div className="spinner" />
      <div>{label}</div>
    </div>
  );
}

const STATUS_COLOR: Record<string, string> = {
  healthy: "var(--positive)",
  connected: "var(--positive)",
  ready: "var(--accent)",
  stale: "var(--warning)",
  disconnected: "var(--negative)",
  auth_error: "var(--negative)",
  invalid_data: "var(--negative)",
};

/** Status dot + (optional) text. Used in System pipeline + Data Health. */
export function HealthDot({ status, pulse }: { status: string; pulse?: boolean }) {
  const color = STATUS_COLOR[status] ?? "var(--neutral)";
  return (
    <span
      className={`health-dot${pulse && status === "healthy" ? " pulse" : ""}`}
      style={{ background: color, boxShadow: `0 0 0 3px ${color}22` }}
      aria-hidden="true"
    />
  );
}
