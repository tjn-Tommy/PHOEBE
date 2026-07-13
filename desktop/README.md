# PHOEBE desktop client

Vue 3 + TypeScript + Tauri 2 shell over the PHOEBE HTTP adapter — the same
`/api/v1` envelope + SSE surface the zero-build web client uses, with a full
dashboard UI (devices, schema-driven run control, live preview, runs catalog
with journal timeline, plugin management, log console, light/dark theme).

## Run it

```powershell
# 1. start the backend (repo root; prints the session token)
python -m phoebe.server --config config/sim.toml

# 2. dev mode (hot reload; needs pnpm + Rust toolchain)
pnpm install
pnpm tauri dev          # or `pnpm dev` for a plain browser tab on :1420

# 3. release build → exe + msi + nsis installers
pnpm tauri build        # outputs under src-tauri/target/release/bundle/
```

On the connect screen enter the server URL (default `http://127.0.0.1:8760`)
and the session token printed by the server.

## Invariants

- `src/api/contracts.d.ts` is **generated** by `tools/gen_ts_types.py`
  (drift-checked in CI together with the sample consumer) — never hand-edit.
- `PINNED_CONTRACTS_VERSION` (`src/stores/connection.ts`) must be bumped
  whenever `CONTRACTS_VERSION` changes; the top bar shows a skew warning
  otherwise.
- Forms are derived from each plugin's JSON Schema (`src/lib/schemaForm.ts`,
  a port of `phoebe/ui/form_model.py`) — defaults have exactly one source
  (H12).
- The Rust side stays empty of `#[tauri::command]`s: every byte of control
  traffic goes through the audited HTTP surface with the Bearer token.
- The API grants CORS only to the fixed desktop/dev origins
  (`phoebe.server.app.DESKTOP_ORIGINS`); auth is header-token, cookie-free.
- `src-tauri/.cargo/config.toml` forces a direct crates.io connection
  because the machine-global git proxy is not always running.
