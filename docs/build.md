# Build & Distribution

---

## Overview

Px is a pure Python application but depends on several packages that have OS and
machine specific binaries. As a result, Px ships two kinds of artifacts:

- **Wheels** — all packages needed to install Px on supported versions of Python.
- **Binary** — compiled binary using Python Embedded on Windows and Nuitka on Mac
  and Linux.

## Supported platforms

Platform coverage is determined by the intersection of native dependency wheels
available from `pymcurl` and `quickjs-ng` on PyPI:

| Platform | Arch | Binary type |
|----------|------|-------------|
| Linux glibc | x86_64, aarch64 | Nuitka |
| Linux musl | x86_64, aarch64 | Nuitka |
| macOS | arm64 | Nuitka |
| Windows | amd64 | Python Embedded |

Each platform produces two archives (`.tar.gz` on Linux/Mac, `.zip` on Windows):
- `px-vX.Y.Z-<os>-<abi>-<arch>` — standalone binary.
- `px-vX.Y.Z-<os>-<abi>-<arch>-wheels` — prebuilt dependency wheels for
  offline `pip install` across Python 3.10–3.14.

## `pyproject.toml`

Package metadata, dependencies, and all tool configuration (ruff, mypy, pytest,
coverage, tox) live in `pyproject.toml`. The build backend is
`setuptools.build_meta`.

## GitHub Actions

All CI and release builds run via GitHub Actions. The workflows live in
`.github/workflows/`.

### CI (`ci.yml`)

Runs on every push to `master` and `devel` branches and on pull requests.

- **quality** — runs `make check` (pre-commit, ruff, mypy).
- **tests** — runs `pytest` on ubuntu, macos, and windows across Python
  3.10–3.14. Each Python version is tested independently. Uses the shared
  `.github/actions/setup-python-env` action for consistent environment setup.
  macOS excludes `test_network.py` due to GitHub Actions environment limitations.

### Build (`build.yml`)

Triggered by version tags (`v*`), pushes to `master` and `devel`, and manual dispatch.
All CI scaffolding (environment setup, wheel building, binary building, archive
extraction, and test execution) is implemented as shell functions in `build.sh`
and called from the workflow steps.

- **sdist** — builds the sdist and pure-Python wheel using `tools.py --wheel`.
- **wheels** — builds dependency wheels for each platform inside manylinux,
  musllinux, or native runners across Python 3.10–3.14. Uses
  `build_wheels` from `build.sh`.
- **binary** — builds Nuitka binaries (Linux/macOS) or the Python Embedded
  distribution (Windows) using `tools.py --nuitka` / `tools.py --embed`.
  Also packages dependency wheel archives with `tools.py --depspkg`.
  Uses `build_binary` from `build.sh`.
  Linux glibc builds run inside manylinux2014 containers using
  `/opt/python/cp313-cp313/bin/python3`. Linux musl builds use Alpine
  containers with system Python and dev headers since Nuitka needs
  `Python.h` which the musllinux containers lack.
- **test-binary** — extracts the release archives produced by the binary job,
  then tests them using `tox` to verify functionality across all Python
  versions (3.10–3.14). Tests run inside Alpine and Ubuntu containers
  on Linux and on native macOS/Windows runners. Uses `extract_archives`
  and `test_binary` from `build.sh`. The `PXBIN` environment variable is
  set so the `binary` tox environment can test the Nuitka binary directly.
  macOS excludes `test_network.py` via `PX_CI_MINIMAL`.
- **release** — collects artifacts and creates a GitHub release with changelog
  notes extracted via `tools.py --history`.
- **publish** — publishes the sdist and wheel to PyPI using trusted publishing.

## `build.sh`

Shell function library sourced by the `build.yml` workflow. It consolidates
repeated CI scaffolding (uv installation, Python discovery, package manager
detection, archive handling) into reusable functions so the workflow YAML
stays concise. Functions include:

- `ensure_uv` — installs uv if not already present.
- `find_python` — locates a Python binary by version (container paths or
  `uv python find --system`).
- `get_os` / `get_version` — detect the current OS flavour and project version.
- `build_wheels` — builds dependency wheels for all supported Python versions.
- `build_binary` — installs build dependencies and runs `tools.py --nuitka` or
  `--embed` plus `--depspkg`.
- `extract_archives` — unpacks binary and wheel archives for the test-binary job.
- `test_binary` — sets up tox and runs the test suite against the built artifacts.

## `tools.py`

Local build helper used by both developers and the GitHub Actions workflows:

- `--wheel` — builds sdist and wheel into `wheel/`.
- `--nuitka` — builds a standalone Nuitka binary for the current platform.
- `--embed` — downloads a Python embeddable distribution, installs the wheel,
  and packages `px.exe` (Windows only).
- `--deps` — builds dependency wheels for the current Python version.
- `--depspkg` — packages all dependency wheels into a release archive with
  sha256 checksums.
- `--docker` — builds `genotrance/px` Docker images (full and mini).
- `--history` — prints the latest changelog section from `docs/changelog.md`
  (used by the release job for GitHub release notes).

## Docker

Px is available as a prebuilt Docker image at `genotrance/px`. Two variants
are posted — the default includes keyring and dependencies, while the mini
version is smaller but requires `PX_PASSWORD` and `PX_CLIENT_PASSWORD`
environment variables for credentials.
