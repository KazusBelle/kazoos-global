import { useState } from "react";
import { login } from "../lib/api";

export function Login({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("kazus");
  const [password, setPassword] = useState("globalocal");
  const [remember, setRemember] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(username.trim(), password, remember);
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
          <div className="relative w-9 h-9 rounded-full flex items-center justify-center text-accent font-bold overflow-hidden">
            <img
              src="/logo_tiger.png?v=20260426-1"
              alt=""
              className="h-full w-full object-contain"
              onError={(e) => {
                e.currentTarget.style.display = "none";
              }}
            />
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
        <label className="flex items-center gap-2 text-xs uppercase tracking-widest text-muted">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            className="h-4 w-4 accent-[#529e79]"
          />
          Remember me
        </label>
        {error && <div className="kz-premium text-sm">{error}</div>}
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
