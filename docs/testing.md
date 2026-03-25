# Testing

---

## Test suite layout

Tests live in `tests/`:

| File | Scope |
|------|-------|
| `conftest.py` | Pytest configuration — path setup, plaintext keyring backend, xdist auto-parallelism hook |
| `fixtures.py` | Shared test fixtures — port allocation, Px server instances (launched with `--verbose`), auth parametrisation |
| `helpers.py` | Utility functions — subprocess management (`run_px` launches Px with `--verbose`), port checks, keyring setup |
| `test_benchmark.py` | Concurrency benchmarks — HTTP/CONNECT throughput, thread count, memory at various concurrency levels (marked `benchmark`, run via `make benchmark`) |
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

# Run with parallel execution (auto-scales to hardware)
uv run python -m pytest tests -n auto
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
3.10–3.14 and a "binary" environment. All environments use `pytest -n auto`
for parallel test execution, auto-scaled by the `conftest.py` hook.

---

## Parallel test execution

Tests use `pytest-xdist` with `-n auto` everywhere — Makefile, tox, and CI.
The `pytest_xdist_auto_num_workers` hook in `conftest.py` computes the worker
count based on CPU count and platform:

- **All platforms**: `max(2, cpu_count // 4)` — each test can spawn up to 4
  processes, so dividing by 4 avoids oversubscription.
- **Windows CI**: forced to 1 (`CI` env var set) — Schannel TLS handshakes fail
  under concurrent HTTPS CONNECT tests when multiple Px instances compete for
  connections on resource-constrained CI runners.

Each xdist worker reserves 3 ports for proxy tests (`fixtures.py`) and 10 ports
for network tests (`test_network.py`), allocated via worker ID offsets to avoid
collisions.

To override the auto-computed value, either pass an explicit `-n N` or set the
`PYTEST_XDIST_AUTO_NUM_WORKERS` environment variable.

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

Test dependencies (`pytest`, `pytest-xdist`, `pytest-httpbin`, `pytest-cov`,
`psutil`) are declared in the `dev` dependency group in `pyproject.toml`
alongside linting and type checking tools (`pre-commit`, `ruff`, `mypy`).
`uv sync` installs them all.

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
both MIT krb5 and Heimdal.

### How it works

Two test classes run against separate KDCs:

**MIT KDC tests** (`TestKerberosIntegration`) — a `kdc` pytest fixture
(module-scoped) starts a throwaway container from the pre-built `px-test-mit-kdc`
image running an MIT KDC with a `TEST.LOCAL` realm and a test principal. Each
test runs `docker run` against the px image with `--network host`, mounts a
generated `krb5.conf`, starts gnome-keyring inside the container, stores a
password via keyring, and then exercises the `KerberosManager` Python code.
Nine tests cover ticket acquisition, renewal, expiry parsing (4-digit year,
2-digit year), klist validity, ccache cleanup, wrong password, bad principal,
and force-retry after failure.

**Heimdal KDC tests** (`TestHeimdalKerberosIntegration`) — a `heimdal_kdc`
fixture starts a container from the pre-built `px-test-heimdal-kdc` image
running a Heimdal KDC, and tests run in the pre-built `px-test-heimdal-client`
image which has px installed from source alongside Heimdal client tools. Five
tests verify ticket acquisition, Heimdal-format expiry parsing (`Mon DD
HH:MM:SS YYYY`), `klist --test` validity check, wrong password handling, and
automatic Heimdal detection via `klist --version`.

All containers use `--network host` so the KDC is accessible at `localhost`
from both the test runner and other containers.

### Docker images

Pre-built Docker images avoid installing packages at container runtime:

- `px-test-mit-kdc` — built from `docker/Dockerfile.mit-kdc` (Alpine +
  krb5-server).
- `px-test-heimdal-kdc` — built from `docker/Dockerfile.heimdal-kdc` (Debian +
  heimdal-kdc).
- `px-test-heimdal-client` — built from `docker/Dockerfile.heimdal-client`
  (python:alpine + Heimdal client + px from source).

KDC setup scripts configure the realm and start the daemon at container
startup.

### Running

Integration tests are marked with `@pytest.mark.integration` and excluded from
the default test run via `addopts` in `pyproject.toml`. To run them locally:

```bash
# Build all Docker images then run the integration tests
make test-kerberos

# Or run them directly (assumes images are already built)
uv run python -m pytest tests/test_kerberos.py -m integration -v
```

### CI

The `kerberos` job in `ci.yml` runs the integration tests on every push to
`devel`/`working` and on pull requests. It builds the Docker images and runs
`make test-kerberos` on `ubuntu-latest`.

### Requirements

- Docker daemon running (containers use `--network host`).
- Pre-built Docker images (`make docker-kerberos`). If images are missing, the
  tests are skipped with a clear message.
- The `--cap-add IPC_LOCK` capability is passed to the px container so that
  `gnome-keyring-daemon` can lock memory pages for secure credential storage.

---

## Concurrency benchmarks

`test_benchmark.py` measures the async server's performance under concurrent
load. The tests are marked with `@pytest.mark.benchmark` and excluded from the
default test run. Benchmarks use `mcurl.Curl` as the HTTP client and a fast
async upstream server (pure asyncio, not httpbin) to ensure the proxy is always
the bottleneck being measured.

### What is measured

- **HTTP GET throughput** (`TestHTTPBenchmark`) — requests per second at
  concurrency 1–200, verifying ≥80% success rate.
- **CONNECT tunnel throughput** (`TestCONNECTBenchmark`) — CONNECT + TLS
  handshake + GET at concurrency 1–200, verifying ≥60% success rate.
- **Thread count bounded** (`TestResourceUsage`) — verifies thread count stays
  constant under 50 concurrent connections (async relay should not spawn threads
  proportional to tunnels).
- **Memory bounded** (`TestResourceUsage`) — verifies RSS does not more than
  double under 200 concurrent requests.
- **Thread pool saturation** (`TestThreadSaturation`) — escalates concurrency
  from 16 to 512 to find the point where the `--threads` pool becomes the
  bottleneck and throughput plateaus.
- **Active data exchange** (`TestActiveDataExchange`) — launches 4–512
  simultaneous CONNECT tunnels that all actively exchange data via a barrier,
  stressing the event loop's FD watcher / IOCP multiplexing.

### Running

```bash
# Via make
make benchmark

# Via pytest directly
uv run python -m pytest tests/test_benchmark.py -m benchmark -v -s
```

Results are printed as a table with columns for concurrency, success/failure
counts, latency percentiles (avg, p50, p99), requests/sec, thread count, and
RSS memory.
