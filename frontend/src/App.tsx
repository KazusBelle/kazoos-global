import { useState } from "react";
import { ChartExportPage } from "./components/ChartExportPage";
import { Dashboard } from "./components/Dashboard";
import { Login } from "./components/Login";
import { getToken } from "./lib/api";

export default function App() {
  // Headless export route — bypasses login. The worker's Playwright runner
  // pre-seeds the auth token into localStorage before navigating here.
  const isExport =
    typeof window !== "undefined" && window.location.pathname === "/chart-export";

  const [authed, setAuthed] = useState<boolean>(!!getToken());

  if (isExport) {
    return <ChartExportPage />;
  }
  if (!authed) {
    return <Login onLogin={() => setAuthed(true)} />;
  }
  return <Dashboard onLogout={() => setAuthed(false)} />;
}
