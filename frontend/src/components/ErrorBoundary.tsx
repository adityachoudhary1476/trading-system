import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): State {
    return { hasError: true };
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
              The application encountered an unexpected error.
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
