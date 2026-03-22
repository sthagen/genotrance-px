# Architecture

---

## Overview

Px is a lightweight HTTP/HTTPS proxy server that enables applications to authenticate through NTLM or Kerberos proxy servers without handling the complex handshake themselves. It leverages Windows SSPI (or equivalent mechanisms on other OSes) to perform single-sign-on using the currently logged-in user credentials. Px runs on Windows, Linux and macOS, either as a Python module installed via `pip` or as a compiled binary built with Nuitka.

## Runtime model

Px uses a multi-process, multi-threaded architecture:

1. **Master process** (`main.py`) — parses config, spawns worker processes.
2. **Worker processes** (`--workers`, default 2) — each runs a `ThreadedTCPServer`
   bound to the configured listen address and port. Workers share the port via
   `SO_REUSEADDR` (Linux/Mac) or socket inheritance (Windows).
3. **Thread pool** (`--threads`, default 32) — each worker has a
   `concurrent.futures.ThreadPoolExecutor`. Incoming connections are dispatched to
   threads via `PoolMixIn.process_request`.
4. **PxHandler** — one instance per connection. Subclass of
   `http.server.BaseHTTPRequestHandler`. Handles both `do_GET/POST/PUT/...` and
   `do_CONNECT` (tunnelling) via `do_curl()`.

```
Client ─── HTTP request ──► PxHandler.do_curl()
                              │
                    ┌─────────▼──────────┐
                    │ get_destination()   │
                    │  STATE.reload_proxy │
                    │  Wproxy.find_proxy  │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │ mcurl.Curl         │
                    │  set_auth()        │
                    │  set_proxy()       │
                    │  perform()         │
                    └─────────┬──────────┘
                              │
                         Response ◄── Upstream proxy / direct
                              │
                    Stream back to client
```

### Mac limitation

macOS does not support `SO_REUSEPORT` in the way needed for multiple processes
to accept on the same socket, so Px is limited to a single worker process on Mac.

## Package layout

| File | Responsibility |
|------|----------------|
| `px/main.py` | Entry point, multiprocessing, TCP server setup, `--test` logic |
| `px/handler.py` | `PxHandler` — request handling, client auth, curl integration, spnego monkey-patching |
| `px/config.py` | `State` singleton, CLI/env/INI/dotenv parsing, proxy reload, `quit`/`restart` actions |
| `px/wproxy.py` | `Wproxy` — proxy discovery from config, environment, Windows Internet Options, PAC |
| `px/pac.py` | `Pac` — PAC file loading and evaluation via quickjs-ng |
| `px/pacutils.py` | Mozilla PAC utility functions injected into the QuickJS runtime |
| `px/debug.py` | `Debug` singleton — stdout/file logging redirection |
| `px/help.py` | CLI help text (rendered from `--help`) |
| `px/kerberos.py` | `KerberosManager` — Kerberos ticket lifecycle (kinit, renewal, cleanup) |
| `px/version.py` | Version string |
| `px/windows.py` | Windows-specific: registry install/uninstall, console attach/detach |

## State singleton (`px.config`)

`State` is a module-level singleton (`STATE = State()`) that holds all runtime
configuration and shared objects. Key attributes:

- **Config fields** — `gateway`, `hostonly`, `listen`, `port`, `noproxy`, `pac`,
  `auth`, `username`, `client_auth`, `client_username`, etc.
- **Shared objects** — `config` (a `configparser.ConfigParser`), `mcurl`
  (a `mcurl.MCurl` instance), `wproxy` (a `Wproxy` instance), `debug`
  (a `Debug` instance).
- **Thread safety** — `state_lock` protects `reload_proxy()` so multiple handler
  threads do not refresh proxy info concurrently.

Configuration is parsed in `parse_config()` which processes the CLI flags,
environment variables (`PX_*`), dotenv files, and `px.ini` in precedence order.
The `DEFAULTS` dict defines fallback values for every config key.

## Request handling (`px.handler`)

`PxHandler.do_curl()` is the central request handler:

1. **Client auth** — if `--client-auth` is enabled, `do_client_auth()` validates
   the client using NEGOTIATE/NTLM/DIGEST/BASIC. NTLM client auth uses
   monkey-patched `spnego._ntlm._get_credential` to look up credentials from
   keyring or `PX_CLIENT_PASSWORD`.
2. **Destination** — `get_destination()` calls `STATE.reload_proxy()` (if the
   `proxyreload` interval has elapsed) and then `Wproxy.find_proxy_for_url()`
   to get the upstream proxy or DIRECT.
3. **Curl setup** — creates/reuses a `mcurl.Curl` object, sets proxy, auth,
   headers, and request body.
4. **Streaming** — response headers and body are streamed back to the client
   via callbacks. CONNECT tunnelling uses `mcurl.Curl` in connect-only mode
   with a select loop for bidirectional data forwarding.

### spnego monkey-patching

`handler.py` replaces `spnego._ntlm._get_credential` and
`spnego._ntlm._get_credential_file` before importing `spnego` itself. This
allows Px to supply NTLM credentials from keyring for client authentication
without requiring a credential file on disk. The import of `spnego` at module
level (line 57) is intentionally after the monkey-patch and is suppressed via
`E402` in ruff.

## Authentication

Px can authenticate to the upstream proxy using:
- **SSPI / GSS-API** when available (Windows default). Detected via
  `mcurl.get_curl_features()`.
- **Username / password** supplied via `--username` and stored in the system
  keyring (`keyring` module) under the realm `Px`. Falls back to `PX_PASSWORD`.
- **Explicit `--auth=NONE`** to defer authentication to the client. In this
  mode, `curl.is_easy = True` to use the easy interface for persistent
  connections needed by NTLM.

Downstream client authentication (gateway mode) is optional and supports
`NEGOTIATE`, `NTLM`, `DIGEST`, and `BASIC`. Credentials are retrieved from the
keyring under the realm `PxClient` or from `PX_CLIENT_PASSWORD` /
`PX_CLIENT_USERNAME`.

## Proxy discovery (`px.wproxy`)

The `Wproxy` class abstracts proxy information from several sources:

| Mode | Source | Trigger |
|------|--------|---------|
| `MODE_CONFIG` | `--proxy` flag | Explicit server list |
| `MODE_CONFIG_PAC` | `--pac` flag | PAC file URL or local path |
| `MODE_ENV` | `http_proxy` / `https_proxy` env vars | No explicit config |
| `MODE_AUTO` | Windows auto-detect (WPAD) | IE proxy config |
| `MODE_PAC` | Windows IE PAC URL | IE proxy config |
| `MODE_MANUAL` | Windows IE manual proxy | IE proxy config |
| `MODE_NONE` | No proxy found | Fallback to DIRECT |

On Windows, the `Wproxy` subclass uses `WinHttpGetIEProxyConfigForCurrentUser()`
and `WinHttpGetProxyForUrl()` via ctypes to discover and resolve proxies from
Internet Options.

### noproxy

The `parse_noproxy()` function parses the noproxy string into two structures:
- `netaddr.IPSet` for IP addresses, ranges (`1.2.3.4-1.2.3.5`), CIDR
  (`10.0.0.0/8`), and wildcards (`192.168.*.*`).
- `set` of hostname strings for domain-based bypasses.

`find_proxy_for_url()` checks both structures before forwarding to the proxy.

## PAC file evaluation (`px.pac`)

The `Pac` class loads a PAC file (from URL or local path) and evaluates it using
[quickjs-ng](https://github.com/nickg/quickjs-ng). The JavaScript runtime is
initialised with Mozilla PAC utility functions from `pacutils.py`
(`dnsDomainIs`, `isInNet`, `shExpMatch`, `myIpAddress`, etc.).

`Pac` uses `quickjs.Function` (rather than `quickjs.Context`) to ensure thread
safety — each call to `find_proxy_for_url()` is dispatched to a thread pool
internally by `quickjs.Function`.

## Proxy reload

`STATE.reload_proxy()` is called on every request. It checks whether
`proxyreload` seconds have elapsed since the last refresh. If so, it acquires
`state_lock` and rebuilds the `Wproxy` instance from the current configuration.
This allows Px to pick up proxy changes (e.g. WPAD updates, PAC file changes)
without restarting.

## Error handling

- **Unhandled exceptions** — `handle_exceptions()` in `main.py` installs a
  global exception handler that writes tracebacks to `debug.log` in the working
  directory.
- **Connection errors** — `PxHandler.handle_one_request()` catches `OSError` and
  `ConnectionError`, logs them, and cleans up the curl handle.
- **Debug module** — `pprint()` and `dprint()` silently swallow exceptions to
  ensure logging never crashes the proxy. Bare excepts in `debug.py` are
  intentional.

## Kerberos ticket management (`px.kerberos`)

On Linux and macOS, upstream Kerberos (NEGOTIATE) authentication requires a
valid TGT in the credential cache. `KerberosManager` handles the full ticket
lifecycle so users do not need external `kinit` scripts.

### MIT vs Heimdal detection

At startup, `KerberosManager` runs `klist --version` and checks whether the
output contains "heimdal". Heimdal's `klist` prints its version string while
MIT's `klist` does not recognise `--version` and exits with an error. The
detection result (`_is_heimdal`) selects the correct flags and date-format
parsers throughout the manager's lifetime. If `klist` is not installed, the
manager defaults to MIT behaviour.

### Inline check pattern

`reload_kerberos()` follows the same pattern as `reload_proxy()`: it is called
on every request from `get_destination()`, uses a timestamp gate for the fast
path, and acquires a blocking lock so concurrent threads wait for renewal
instead of proceeding with an expired ticket.

### Per-process isolation

Each worker process creates its own `KerberosManager` with an isolated
credential cache (`KRB5CCNAME=FILE:/tmp/krb5cc_px_<pid>`). Since workers call
`parse_config()` independently after `spawn`, each gets its own instance with
no shared state.

### GSS-API startup check

`parse_config()` verifies that libcurl was built with GSS-API support before
creating a `KerberosManager`. If the feature is missing, px exits with an
error. SSPI (Windows) is not checked because `--kerberos` only applies to
Linux and macOS.

### GSS-API path override

When `--kerberos` is enabled, `set_curl_auth()` forces `key = ":"` (GSS-API
mode) regardless of whether `--username` is set. The username and password are
used only by `KerberosManager` for `kinit`, not passed to libcurl.

### PTY-based password piping

`kinit` reads passwords from `/dev/tty`, not stdin. `KerberosManager` uses
`pty.openpty()` to create a pseudo-terminal pair, makes the slave side the
child process's controlling terminal via `setsid` + `TIOCSCTTY`, then writes
the password on the master side so that kinit's `read(/dev/tty)` sees it.
No keytab file is created — credentials stay in memory.

### Auth failure recovery

When libcurl reports an SSO failure (`resp == 401`, "single sign-on failed") or
a mechanism error (`resp == 407`, "auth mechanism error"), the reactive check
forces a ticket renewal bypassing both the fast-path timestamp gate and the
in-lock double-check. If the ticket is invalid, a new one is acquired and
`MCURL.failed` is cleared so previously-blocked proxies are retried.

### Credential cache cleanup

`atexit` registers `_cleanup()` to remove the per-process ccache file.
`do_quit()` calls `cleanup_kerberos()` explicitly before `os._exit()` since
`os._exit()` bypasses `atexit` handlers.
