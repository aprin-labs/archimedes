import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import 'virtual:uno.css'
import './App.css'
import App from './App.jsx'
import { AuthProvider } from './AuthContext.jsx'
import ConsentBanner from './components/ConsentBanner.jsx'
import ErrorBoundary from './components/ErrorBoundary.jsx'

// ConsentBanner mounts here, as a sibling of App rather than inside any
// layout, for two reasons (#1647): every surface gets it (public pages, the
// auth screens, the app shell — App returns a different tree for each), and
// it stays inside the ErrorBoundary so a fault in the banner degrades to the
// fallback instead of taking the product down. It renders null once a choice
// is recorded.
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <AuthProvider>
      <ErrorBoundary>
        <App />
        <ConsentBanner />
      </ErrorBoundary>
    </AuthProvider>
  </StrictMode>,
)
