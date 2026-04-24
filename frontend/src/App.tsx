import { useState } from "react";
import { Dashboard } from "./components/Dashboard";
import { Login } from "./components/Login";
import { getToken } from "./lib/api";

export default function App() {
  const [authed, setAuthed] = useState<boolean>(!!getToken());
  if (!authed) {
    return <Login onLogin={() => setAuthed(true)} />;
  }
  return <Dashboard onLogout={() => setAuthed(false)} />;
}
