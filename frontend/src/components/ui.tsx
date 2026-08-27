import type { ReactNode } from "react";

export function Panel({
  title,
  actions,
  children,
  className,
}: {
  title?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel${className ? ` ${className}` : ""}`}>
      {title && (
        <div className="panel-head">
          <span className="panel-title">{title}</span>
          {actions}
        </div>
      )}
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
