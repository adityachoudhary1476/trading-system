import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { fetchConnectionStatus, startUpstoxOAuth, disconnectUpstox } from "@/lib/upstox";
import { Panel, Button, Feedback, Loading, EmptyState } from "@/components/ui";
import type { ConnectionStatus } from "@/lib/upstox";

export function BrokerConnectionsPage() {
  const { user, loading: authLoading, signIn, signOut } = useAuth();
  const [searchParams] = useSearchParams();
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connected = searchParams.get("connected");

  useEffect(() => {
    if (!user) {
      setStatus(null);
      return;
    }
    let alive = true;
    setLoading(true);
    fetchConnectionStatus()
      .then((s) => alive && setStatus(s))
      .catch(() => alive && setStatus({ connected: false }))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [user, connected]);

  async function handleConnect() {
    setBusy(true);
    setError(null);
    try {
      await startUpstoxOAuth();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start OAuth flow.");
      setBusy(false);
    }
  }

  async function handleDisconnect() {
    setDisconnecting(true);
    setError(null);
    try {
      await disconnectUpstox();
      setStatus({ connected: false });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to disconnect.");
    } finally {
      setDisconnecting(false);
    }
  }

  if (authLoading) {
    return <Loading label="Checking session…" />;
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1 className="page-title">Broker Connections</h1>
          <p className="subtitle">
            Link your broker accounts. Credentials are exchanged server-side and stored encrypted.
          </p>
        </div>
      </div>

      {connected === "success" && (
        <Feedback kind="success">Upstox connected successfully.</Feedback>
      )}
      {connected === "error" && (
        <Feedback kind="error">
          Upstox connection failed. Please try again or contact support.
        </Feedback>
      )}
      {error && <Feedback kind="error">{error}</Feedback>}

      {!user ? (
        <Panel title="Sign in required">
          <EmptyState
            title="Sign in to connect a broker"
            hint="Create an account or sign in, then connect Upstox."
            icon="◩"
          />
          <SignInForm signIn={signIn} />
        </Panel>
      ) : (
        <div className="grid cols-2" style={{ gap: 16 }}>
          <Panel
            title="Upstox"
            actions={
              status?.connected ? (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleDisconnect}
                  disabled={disconnecting}
                >
                  {disconnecting ? "Disconnecting…" : "Disconnect"}
                </Button>
              ) : null
            }
          >
            {loading ? (
              <Loading label="Checking connection…" />
            ) : status?.connected ? (
              <div>
                <Feedback kind="success">Connected</Feedback>
                {status.obtained_at && (
                  <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>
                    Connected {new Date(status.obtained_at).toLocaleString()}
                  </div>
                )}
              </div>
            ) : (
              <div>
                <EmptyState
                  title="Not connected"
                  hint="Connect Upstox to enable live market data and trading."
                  icon="○"
                />
                <div style={{ marginTop: 12 }}>
                  <Button
                    variant="primary"
                    onClick={handleConnect}
                    disabled={busy}
                  >
                    {busy ? "Redirecting…" : "Connect Upstox"}
                  </Button>
                </div>
              </div>
            )}
          </Panel>

          <Panel title="Account">
            <div className="stat">
              <span className="label">Signed in as</span>
              <span className="value">{user.email}</span>
            </div>
            <div style={{ marginTop: 12 }}>
              <Button variant="secondary" size="sm" onClick={signOut}>
                Sign out
              </Button>
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}

function SignInForm({ signIn }: { signIn: (email: string, password: string) => Promise<{ error: string | null }> }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    const { error } = await signIn(email, password);
    setBusy(false);
    if (error) setErr(error);
  }

  return (
    <form onSubmit={submit} style={{ marginTop: 12 }}>
      {err && (
        <div style={{ marginBottom: 8 }}>
          <Feedback kind="error">
            {err}
          </Feedback>
        </div>
      )}
      <input
        type="email"
        placeholder="Email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        required
        style={{ width: "100%", marginBottom: 8 }}
      />
      <input
        type="password"
        placeholder="Password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        required
        style={{ width: "100%", marginBottom: 8 }}
      />
      <Button variant="primary" type="submit" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}
