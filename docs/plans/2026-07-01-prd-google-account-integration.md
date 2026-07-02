# PRD — Pillar 2: Google Account Integration

**Version:** 0.1 (draft for review)
**Date:** 2026-07-01
**Status:** Planning. Part of the [Meeting Notes Desktop App epic](2026-07-01-meeting-app-epic.md).
**Depends on:** Pillar 1 app shell (tray + Preferences window). **Prerequisite for:** [Pillar 3](2026-07-01-prd-auto-detect-record.md).

---

## Executive Summary

Let a user **connect one or more Google accounts** to the app via standard
desktop OAuth, so the app can (a) read their **calendar** to power meeting
auto-detection (Pillar 3) and (b) know **which accounts/meetings** exist for
labeling and future per-user features. Tokens are stored in the **OS keychain**,
refreshed automatically, and revocable from Preferences. This pillar is pure
Python/HTTPS — **no per-OS code** beyond what the `keyring` library already
abstracts.

---

## Goals

- Connect **multiple** Google accounts (work + personal) and list them in Prefs.
- Read-only **calendar** access to enumerate upcoming events and their Meet links.
- Basic **identity** (email, display name) per account for labeling.
- Secure token storage (OS keystore), automatic refresh, one-click disconnect.
- Fully cross-platform with a single code path.

## Non-Goals

- No Gmail/Drive/Docs access in this pillar (calendar + identity only; least
  privilege). Additional scopes are a later, separate ask.
- No writing to the calendar (read-only).
- No detection/recording logic — that's Pillar 3; this pillar only supplies
  authenticated calendar data.

---

## OAuth Design (desktop app)

- **Flow:** OAuth 2.0 **Authorization Code with PKCE**, using the **loopback
  redirect** method Google recommends for desktop apps. On "Connect Google," the
  app:
  1. spins up a temporary `http://127.0.0.1:<random-port>` listener,
  2. opens the system browser to Google's consent screen,
  3. receives the code on the loopback redirect, exchanges it (with PKCE) for
     access + refresh tokens.
- **Libraries:** `google-auth`, `google-auth-oauthlib`,
  `google-api-python-client` — all cross-platform, no native code. The loopback
  server uses the stdlib; opening the browser uses `webbrowser` (works on both
  OSes).
- **Why loopback, not a client secret embedded as-is:** desktop apps are public
  clients; PKCE + loopback is the current Google-sanctioned pattern and avoids
  shipping a usable secret. (A client *ID* is shipped; that's expected.)

### Scopes (least privilege)

| Scope | Why |
|---|---|
| `.../auth/calendar.readonly` | Enumerate events + Meet links for detection (Pillar 3). |
| `.../auth/userinfo.email`, `openid` | Identify the connected account; distinguish multiple accounts. |

We request only these. Broadening scope later is an explicit, re-consented step.

---

## Multiple Accounts

- Each connected account is stored as its own token record keyed by email.
- Preferences lists accounts with status (connected / needs re-auth) and a
  **Disconnect** action (revokes + deletes local tokens).
- Pillar 3 polls **all** connected calendars and merges events (dedup by event
  id + Meet URL).

---

## Token Storage & Security

- **Secrets in the OS keystore via `keyring`:** Windows Credential Manager,
  macOS Keychain — one API, no per-OS code. Refresh tokens **never** touch plain
  files.
- **Non-secret metadata** (email, display name, scopes, last-sync) lives in the
  app config dir (`platformdirs`).
- **Refresh:** access tokens refreshed on demand using the stored refresh token;
  failures surface a "reconnect" prompt in Prefs rather than silently breaking
  detection.
- **Revocation:** Disconnect calls Google's revoke endpoint and purges the
  keystore entry.
- **Privacy posture:** calendar data is read transiently to schedule/detect; we
  persist only what a meeting needs (title, time, Meet URL, account) into
  `meta.json` when a recording is actually made. No calendar mirror on disk.

---

## Data Flow

```
Preferences ──"Connect Google"──► loopback OAuth (PKCE) ──► tokens ──► keyring
                                                              │
Pillar 3 watcher ──► google-api-python-client (calendar.readonly) ──┘
        │  merge events across accounts, extract Meet URLs
        ▼
   upcoming-meeting model (in memory) ──► Record popup (Pillar 3)
```

---

## Cross-Platform Considerations

| Concern | Mac | Windows | Shared? |
|---|---|---|---|
| OAuth loopback + browser open | stdlib + `webbrowser` | same | ✅ |
| Google API calls | HTTPS | HTTPS | ✅ |
| Token storage | Keychain via `keyring` | Credential Mgr via `keyring` | ✅ (one API) |
| Prefs UI | Qt | Qt | ✅ |

**No per-OS code in this pillar.** `keyring` is the only place the OS differs,
and it hides that behind one interface.

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Google OAuth **verification/branding** review for sensitive scopes | Medium | `calendar.readonly` is sensitive; budget for Google's app-verification process before public release. Pre-verification, works for test users. |
| `keyring` has no backend on a stripped-down OS | Low | Detect and warn; fall back to an encrypted local store only with explicit user consent. |
| Refresh token revoked server-side (password change, etc.) | Medium | Detect 401/invalid_grant → mark account "needs re-auth," prompt in Prefs; never crash the watcher. |
| Loopback port blocked by firewall | Low | Random high port + retries; clear error with guidance. |
| Scope creep (Gmail/Drive requests sneaking in) | Medium | Hard-limit to the two scopes above; any addition is its own PRD + re-consent. |

---

## Acceptance Criteria

- User can connect ≥2 Google accounts; both appear in Preferences with correct
  email/status.
- App lists upcoming events (with Meet URLs) across all connected calendars.
- Tokens survive app restart (loaded from keystore) and auto-refresh.
- Disconnect revokes access and removes local credentials.
- Verified on both macOS and Windows with no platform-specific branches beyond
  `keyring`'s internals.

---

## Phasing

1. **2a:** Single-account connect, calendar read, keystore storage, Prefs status.
2. **2b:** Multiple accounts + merge/dedup + disconnect/revoke polish.

---

## Open Questions

1. Ship as a **Google-verified** app before public launch (needed for sensitive
   scopes at scale), or start with test-user allowlist?
2. Do we ever need `calendar.events.readonly` granularity vs full
   `calendar.readonly`? (Leaning: the narrower one if it covers Meet URLs.)
3. Should identity also feed a future "who am I in this meeting" attribution, or
   is that out of scope indefinitely?
