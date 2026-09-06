// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./index.css";

/* Office.js initialises asynchronously. We wait for it before mounting React
   so the API is available when any component first renders. */
Office.onReady(() => {
  createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
});
