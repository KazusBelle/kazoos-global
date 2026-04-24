import { useState } from "react";
import { login } from "../lib/api";

export function Login({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("admin");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(username.trim(), password);
      onLogin();
    } catch (err: any) {
      setError(err.message ?? "login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center">
      <form
        onSubmit={submit}
        className="w-full max-w-sm bg-panel border border-border rounded-2xl p-8 space-y-4 shadow-2xl"
      >
        <div className="flex items-center gap-3 mb-2">
          <div className="w-9 h-9 rounded-full border-2 border-accent flex items-center justify-center text-accent font-bold">
            K
          </div>
          <div>
            <div className="text-sm uppercase tracking-widest text-muted">Kazus</div>
            <div className="text-lg font-semibold">Screener</div>
          </div>
        </div>
        <label className="block">
          <span className="text-xs uppercase tracking-widest text-muted">Username</span>
          <input
            className="mt-1 w-full bg-bg border border-border rounded-lg px-3 py-2 font-mono focus:outline-none focus:border-accent"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
          />
        </label>
        <label className="block">
          <span className="text-xs uppercase tracking-widest text-muted">Password</span>
          <input
            type="password"
            className="mt-1 w-full bg-bg border border-border rounded-lg px-3 py-2 font-mono focus:outline-none focus:border-accent"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && <div className="text-premium text-sm">{error}</div>}
        <button
          type="submit"
          disabled={loading}
          className="w-full bg-accent text-black rounded-lg py-2 font-semibold uppercase tracking-widest disabled:opacity-50"
        >
          {loading ? "…" : "Enter"}
        </button>
      </form>
    </div>
  );
}
