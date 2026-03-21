# Testing

---

## Test suite layout

Tests live in `tests/`:

| File | Scope |
|------|-------|
| `conftest.py` | Pytest configuration — path setup and plaintext keyring backend |
| `fixtures.py` | Shared test fixtures — port allocation, Px server instances, auth parametrisation |
| `helpers.py` | Utility functions — subprocess management, port checks, keyring setup |
| `test_config.py` | Configuration utility tests — `get_logfile`, `get_config_dir`, `get_host_ips`, defaults, save, install |
| `test_debug.py` | Debug module tests — `Debug` singleton, `pprint`, `dprint` |
| `test_kerberos.py` | Kerberos ticket management — unit tests (mocked subprocess, Linux/macOS only) and Docker-based integration tests against local MIT and Heimdal KDCs (marked `integration`, run via `make test-kerberos`) |
| `test_network.py` | Network integration tests — `--quit`, `--listen`, `--hostonly`, `--gateway`, `--allow`, `--noproxy` |
| `test_pac.py` | PAC file tests — loading, evaluation, encoding, JS callables (`dnsResolve`, `myIpAddress`) |
| `test_proxy.py` | Proxy functionality tests — HTTP methods, auth, upstream auth, chaining |
| `test_wproxy.py` | Proxy parsing tests — `parse_proxy`, `parse_noproxy`, `_WproxyBase` methods |

---

## Running tests

### Quick run

```bash
make test
```

This runs `pytest` with coverage via `uv run`.

### Manual run

```bash
# Install dev dependencies
uv sync
uv pip install -e .

# Run all tests
uv run python -m pytest tests -q

# Run a specific file
uv run python -m pytest tests/test_proxy.py -q

# Run with coverage
uv run python -m pytest tests --cov --cov-config=pyproject.toml --cov-report=xml

# Run with parallel execution
uv run python -m pytest tests -n 4
```

### With a specific Python version

```bash
uv run -p 3.14 python -m pytest tests -q
```

### Full test matrix via tox

```bash
uv run -p 3.13 tox
```

The `tox` configuration in `pyproject.toml` defines environments for Python
3.10–3.14 and a "binary" environment.

---

## CI testing

GitHub Actions runs the full test suite on every push to the `devel` branch and
on pull requests via `.github/workflows/ci.yml`. The matrix covers 9
configurations: Ubuntu on Python 3.10–3.14, macOS on 3.10 and 3.14, and Windows
on 3.10 and 3.14. All Python versions (3.10–3.14) are additionally tested via
tox in the build workflow's `test-binary` job.

The build workflow (`.github/workflows/build.yml`) triggers on pushes to
`master` and manual dispatch. It tests built artifacts using tox across all
Python versions (3.10–3.14) inside musllinux and Ubuntu Docker containers and on
native macOS/Windows runners.

---

## Local container testing

The `build_local` function in `build.sh` provides end-to-end local build and
test using Docker containers. It builds the sdist on the host, then runs the
wheels, binary, and test steps inside appropriate container images.

```bash
# Build and test in musl (Alpine) containers
make test-musl

# Build and test in glibc (manylinux) containers
make test-glibc
```

This matches the CI pipeline closely and is useful for verifying Linux builds
locally before pushing.

---

## Reduced test matrix for macOS CI

macOS GitHub Actions runners are significantly slower than Linux/Windows runners
for the chain and upstream proxy tests. These tests spawn multiple Px processes
and involve real network authentication flows that take much longer on macOS GHA
than on local hardware. To keep CI times reasonable, macOS uses a reduced test
matrix controlled by the `PX_CI_MINIMAL` environment variable.

When `PX_CI_MINIMAL=1` is set:

1. **Auth/env pairing**: Instead of testing all combinations of auth types (NTLM,
   DIGEST, BASIC) with all CLI/env modes, we use strategic pairing:
   - NTLM + cli
   - DIGEST + env
   - BASIC + cli

   This maintains coverage of all auth types and both configuration modes while
   reducing combinations from 6 to 3.

2. **Skip chain tests**: `test_proxy_auth_upstream` and `test_proxy_auth_chain`
   are skipped entirely as they spawn multiple Px processes and are too slow for
   GitHub Actions macOS runners.

3. **Network tests excluded**: `test_network.py` is excluded on macOS CI as these
   tests fail in the GitHub Actions environment but pass on real macOS hardware.

**Result**: The test count drops from 186 to 24 tests (87% reduction) while
maintaining full auth diversity (NTLM, DIGEST, BASIC) and both config modes (cli, env).

The pairing logic is implemented in `tests/fixtures.py` via `PARAMS_AUTH_PAIRED`
that conditionally modifies fixture parametrization based on the `PX_CI_MINIMAL`
environment variable. Chain tests are skipped using `@pytest.mark.skipif` decorators
in `tests/test_proxy.py`.

---

## Keyring backend for testing

Tests use the plaintext keyring backend to avoid system keyring prompts and ensure
consistent behavior across platforms. This is set globally in `conftest.py` which:

- Sets `PX_KEYRING_PLAINTEXT=1` environment variable for all test runs
- Configures `keyring.set_keyring(keyrings.alt.file.PlaintextKeyring())`

The plaintext backend stores passwords unencrypted in a file, which is acceptable
for testing but not for production use. This configuration is inherited by all
tests including those run via `tox`.

---

## Test dependencies

Test dependencies (`pytest`, `pytest-xdist`, `pytest-httpbin`, `pytest-cov`) are
declared in the `dev` dependency group in `pyproject.toml` alongside linting and
type checking tools (`pre-commit`, `ruff`, `mypy`). `uv sync` installs them all.

---

## Coverage

Coverage is configured in `pyproject.toml` under `[tool.coverage.*]`. Branch
coverage is enabled and scoped to the `px` package. Empty files are skipped
in reports.

---

## Kerberos integration tests

The unit tests in `test_kerberos.py` mock all subprocess calls to verify the
`KerberosManager` logic in isolation. The same file also contains Docker-based
integration tests that exercise the real Kerberos stack against local KDCs —
both MIT krb5 (Linux) and Heimdal (macOS).

### How it works

Two test classes run against separate KDCs:

**MIT KDC tests** (`TestKerberosIntegration`) — a `kdc` pytest fixture
(module-scoped) starts a throwaway Alpine container running an MIT KDC with a
`TEST.LOCAL` realm and a test principal. Each test runs `docker run` against
the px image, mounts a generated `krb5.conf` pointing at the KDC container's
IP, starts gnome-keyring inside the container, stores a password via keyring,
and then exercises the `KerberosManager` Python code. Nine tests cover ticket
acquisition, renewal, expiry parsing (2-digit year), klist validity, ccache
cleanup, wrong password, bad principal, and force-retry after failure.

**Heimdal KDC tests** (`TestHeimdalKerberosIntegration`) — a `heimdal_kdc`
fixture starts a Debian container running a Heimdal KDC, and a
`heimdal_client_image` fixture builds a temporary Docker image with px
installed from source alongside Heimdal client tools (instead of MIT krb5).
Five tests verify ticket acquisition, Heimdal-format expiry parsing (`Mon DD
HH:MM:SS YYYY`), `klist --test` validity check, and wrong password handling.
This ensures px works correctly with macOS-style Kerberos output.

### Running

Integration tests are marked with `@pytest.mark.integration` and excluded from
the default test run via `addopts` in `pyproject.toml`. They are also skipped
when the `CI` environment variable is set to `true` (GitHub Actions). To run
them locally:

```bash
# Build the px Docker images then run only the integration tests
make test-kerberos

# Or run them directly (assumes images are already built)
uv run python -m pytest tests/test_kerberos.py -m integration -v
```

The KDC containers, Heimdal client image, and all temp files are cleaned up
automatically by fixture teardown.

### Requirements

- Docker with access to the default bridge network (containers must be able to
  reach each other by IP).
- A locally-built px image (`make docker`). If the image is missing, the MIT
  tests are skipped with a clear message. The Heimdal tests build their own
  client image from source automatically.
- The `--cap-add IPC_LOCK` capability is passed to the px container so that
  `gnome-keyring-daemon` can lock memory pages for secure credential storage.
