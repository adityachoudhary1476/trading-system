import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  errorMessage: string | null;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, errorMessage: null };
  }

  static getDerivedStateFromError(error: unknown): State {
    const errorMessage = error instanceof Error ? error.message : String(error);
    return { hasError: true, errorMessage };
  }

  componentDidCatch(error: unknown, errorInfo: React.ErrorInfo): void {
    const timestamp = new Date().toISOString();
    const pathname = typeof window !== "undefined" ? window.location.pathname : "unknown";
    const errorMessage = error instanceof Error ? error.message : String(error);
    const stack = error instanceof Error ? error.stack ?? "No stack trace" : "No stack trace";
    const componentStack = errorInfo.componentStack ?? "No component stack";

    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary]", {
      timestamp,
      pathname,
      error: errorMessage,
      stack,
      componentStack,
    });
  }

  render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: "100vh",
            padding: 24,
            textAlign: "center",
          }}
        >
          <div>
            <h2 style={{ marginBottom: 8 }}>Something went wrong</h2>
            <p style={{ color: "var(--text-faint, #888)", marginBottom: 16 }}>
              Something went wrong. Please reload the page.
            </p>
            <button
              onClick={() => window.location.reload()}
              style={{
                padding: "8px 16px",
                cursor: "pointer",
                background: "var(--accent, #4a9eff)",
                color: "#fff",
                border: "none",
                borderRadius: 6,
              }}
            >
              Reload page
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
