import PublicLayout from './PublicLayout'

// Extracted from App.jsx's route.kind === 'not-found' branch so a SECOND
// caller — the /app/insights admin gate (owner directive 2026-08-20,
// supersedes issue #1028 D8) — can render the exact same "does not exist"
// treatment for a non-admin/anonymous visitor. "Exact same" is the point:
// a page that instead said "you need admin access" would itself advertise
// that /app/insights exists and is worth probing further. Any change here
// changes both the true 404 and the admin gate's denial screen identically
// — that is intentional, not a risk to route around with a second copy.
// This closes ONLY the denial-screen-wording vector, not concealment in
// general — see ui/src/adminProbe.js's "Scope of 'does not advertise the
// page exists'" note for what still leaks (the probe itself, the bundled
// route/component).
export default function NotFound({ user }) {
  return (
    <PublicLayout user={user}>
      <main className="min-h-[70vh] flex flex-col items-center justify-center gap-4 px-4 text-center">
        <h1 className="serif text-[2rem]">Page not found</h1>
        <a className="btn-primary" href={user ? '/app' : '/'}>{user ? 'Open app' : 'Go home'}</a>
      </main>
    </PublicLayout>
  )
}
