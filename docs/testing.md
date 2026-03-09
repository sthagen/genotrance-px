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

GitHub Actions runs the full test suite on every push and pull request via
`.github/workflows/ci.yml`. The matrix covers ubuntu, macos, and windows on
Python 3.14. All Python versions (3.10–3.14) are tested via tox in the build
workflow's `test-binary` job.

The build workflow (`.github/workflows/build.yml`) additionally tests built
artifacts using tox across all Python versions (3.10–3.14) inside Alpine,
Ubuntu, and Debian Docker containers and on native macOS/Windows runners.

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
