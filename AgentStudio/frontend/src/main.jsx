// SPDX-License-Identifier: MIT
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import BuildStudio from './BuildStudio.jsx'

// Standalone bootstrap. When embedding into a host app, import BuildStudio
// directly and render it at a route instead of using this file.
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BuildStudio />
  </StrictMode>,
)
