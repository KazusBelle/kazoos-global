import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { FrontendErrorBoundary, installFrontendErrorReporter } from "./lib/frontendErrors";
import "./index.css";

document.documentElement.dataset.motion =
  localStorage.getItem("kazus_motion") === "0" ? "off" : "on";
installFrontendErrorReporter();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <FrontendErrorBoundary>
      <App />
    </FrontendErrorBoundary>
  </React.StrictMode>
);
