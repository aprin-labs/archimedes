import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { _resetAdminProbeCache } from './adminProbe.js'
import { _resetInsightsAdminMemo } from './insightsAdminMemo.js'
import { getSession, signOut as endSession } from './auth-client'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [session, setSession] = useState(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      setSession(await getSession())
    } catch {
      setSession(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const signOut = useCallback(async () => {
    await endSession()
    setSession(null)
    // A cached "admin" determination must not survive into whatever
    // signs in next on this browser (owner directive 2026-08-20 admin
    // gate) — the shared probe cache is keyed on nothing session-specific,
    // so it has to be cleared explicitly here rather than expiring on its
    // own short TTL.
    _resetAdminProbeCache()
    // Same reason, for the per-session memo of the last answer (#1648): it is
    // keyed on the account id, so it CANNOT be read by a different account
    // signing in next — but an admin signing out and back in should still get
    // a fresh determination rather than seeing the previous session's grant
    // painted before the server has re-confirmed it. Defence in depth, not the
    // load-bearing isolation (that is the key — insightsAdminMemo.js).
    _resetInsightsAdminMemo()
  }, [])

  const value = useMemo(() => ({
    user: session?.user ?? null,
    session: session?.session ?? null,
    loading,
    refresh,
    signOut,
  }), [session, loading, refresh, signOut])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside AuthProvider')
  return value
}
