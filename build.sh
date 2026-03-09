#!/bin/sh
# CI helper functions for build.yml
# Usage: . ./build.sh && <function> [args]

set -e

# --- Common helpers ---

get_os() {
    case "$(uname -s)" in
        Linux)
            if ldd /bin/ls 2>/dev/null | grep -q musl; then
                echo "linux-musl"
            else
                echo "linux-glibc"
            fi
            ;;
        Darwin) echo "mac" ;;
        MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
        *) echo "unsupported" ;;
    esac
}

get_version() {
    grep '^version' pyproject.toml | head -1 | cut -d'"' -f2
}

ensure_uv() {
    if command -v uv > /dev/null 2>&1; then
        return
    fi
    curl -LsSf https://astral.sh/uv/install.sh | sh
    . ~/.local/bin/env
}

# Find Python binary for a given version
# Usage: find_python 313  (compact) or find_python 3.13 (dotted)
find_python() {
    pyver="$1"
    if [ -d "/opt/python" ]; then
        # manylinux/musllinux container
        echo "/opt/python/cp${pyver}-cp${pyver}/bin/python3"
    else
        # Native runner — use uv (--system skips venvs)
        uv python find --no-project --system "$pyver" 2>/dev/null || echo ""
    fi
}

# --- Wheel building ---

build_wheels() {
    ensure_uv

    if [ -d "/opt/python" ]; then
        # manylinux/musllinux container — compact version numbers
        for pyver in 310 311 312 313 314; do
            PY=$(find_python "$pyver")
            if [ -f "$PY" ]; then
                echo "Building wheels for Python $pyver"
                "$PY" -m pip wheel . -w wheels
            fi
        done
    else
        # Native runner (macOS/Windows) — dotted version numbers
        for pyver in 3.10 3.11 3.12 3.13 3.14; do
            uv python install "$pyver" || continue
            PY=$(find_python "$pyver") || continue
            if [ -n "$PY" ]; then
                echo "Building wheels for Python $pyver"
                "$PY" -m pip wheel . -w wheels
            fi
        done
    fi
}

# --- Binary building ---

# Install upx per platform
install_upx() {
    OS=$(get_os)
    case "$OS" in
        linux-musl)
            apk add --no-cache upx 2>/dev/null || true
            ;;
        linux-glibc)
            yum install -y upx 2>/dev/null || dnf install -y upx 2>/dev/null || true
            ;;
        mac)
            brew install upx
            ;;
        windows)
            choco install upx -y
            ;;
    esac
}

# Install system build dependencies for Alpine
install_alpine_deps() {
    apk add --no-cache curl python3 python3-dev py3-pip gcc musl-dev \
        patchelf libffi-dev ccache upx
}

# Set up manylinux static libs
setup_manylinux() {
    if [ -f "/opt/_internal/static-libs-for-embedding-only.tar.xz" ]; then
        (cd /opt/_internal && tar xf static-libs-for-embedding-only.tar.xz)
    fi
}

build_binary() {
    NAME="$1"
    WHEELS="px.dist-${NAME}-wheels/px.dist"
    OS=$(get_os)

    case "$OS" in
        linux-musl)
            install_alpine_deps
            ensure_uv
            python3 -m venv .venv
            . .venv/bin/activate
            uv pip install nuitka pymcurl -f "$WHEELS"
            uv pip install px-proxy --no-index -f "$WHEELS"
            python tools.py --nuitka
            python tools.py --depspkg
            ;;
        linux-glibc)
            install_upx
            setup_manylinux
            ensure_uv
            PY=/opt/python/cp313-cp313/bin/python3
            "$PY" -m venv .venv
            . .venv/bin/activate
            uv pip install nuitka pymcurl -f "$WHEELS"
            uv pip install px-proxy --no-index -f "$WHEELS"
            python tools.py --nuitka
            python tools.py --depspkg
            ;;
        mac)
            install_upx
            ensure_uv
            PY=$(uv python find 3.13)
            uv pip install nuitka pymcurl -f "$WHEELS"
            "$PY" tools.py --nuitka
            "$PY" tools.py --depspkg
            ;;
        windows)
            install_upx
            ensure_uv
            PY=$(uv python find 3.13)
            uv pip install pymcurl -f "$WHEELS"
            "$PY" tools.py --embed
            "$PY" tools.py --depspkg
            ;;
    esac
}

# --- Archive extraction ---

extract_archives() {
    NAME="$1"

    # Extract binary archive into px.dist-{name}/px.dist/
    mkdir -p "px.dist-${NAME}/px.dist"
    for f in "px.dist-${NAME}"/px-v*-"${NAME}".tar.gz; do
        [ -f "$f" ] && tar xf "$f" -C "px.dist-${NAME}/px.dist"
    done
    for f in "px.dist-${NAME}"/px-v*-"${NAME}".zip; do
        [ -f "$f" ] && unzip -q "$f" -d "px.dist-${NAME}/px.dist"
    done

    # Extract wheels archive into px.dist-{name}-wheels/px.dist/
    mkdir -p "px.dist-${NAME}-wheels/px.dist"
    for f in "px.dist-${NAME}-wheels"/px-v*-wheels.tar.gz; do
        [ -f "$f" ] && tar xf "$f" -C "px.dist-${NAME}-wheels/px.dist"
    done
    for f in "px.dist-${NAME}-wheels"/px-v*-wheels.zip; do
        [ -f "$f" ] && unzip -q "$f" -d "px.dist-${NAME}-wheels/px.dist"
    done

    # Debug: show what was extracted
    ls -la "px.dist-${NAME}/px.dist/" || true
    ls -la "px.dist-${NAME}-wheels/px.dist/" || true
}

# --- Test binary ---

test_binary() {
    NAME="$1"
    OS=$(get_os)

    # Set paths
    PXBIN="px.dist-${NAME}/px.dist/px"
    if [ "$OS" = "windows" ]; then
        PXBIN="px.dist-${NAME}/px.dist/px.exe"
    fi
    WHEELS="px.dist-${NAME}-wheels/px.dist"
    PXWHEEL=""
    for whl in "$WHEELS"/px_proxy*.whl; do
        [ -f "$whl" ] && PXWHEEL="$whl" && break
    done
    if [ -z "$PXWHEEL" ]; then
        echo "ERROR: No px_proxy wheel found in $WHEELS"
        return 1
    fi

    if [ "$OS" = "linux-musl" ] || [ "$OS" = "linux-glibc" ]; then
        # Inside container — install curl if needed (before ensure_uv)
        if ! command -v curl > /dev/null 2>&1; then
            if command -v apk > /dev/null; then
                apk add --no-cache curl
            else
                apt-get update -qq
                apt-get install -y -qq curl > /dev/null
            fi
        fi

        ensure_uv

        # Run tests with tox in a venv
        uv venv /tmp/tox-env
        . /tmp/tox-env/bin/activate
        uv pip install tox tox-uv
        PXBIN="$PXBIN" tox --installpkg "$PXWHEEL" \
            --override "tool.tox.env_run_base.install_command=uv pip install --no-index -f $WHEELS" \
            --workdir /tmp
    else
        # Native runner (macOS/Windows)
        ensure_uv
        PYTEST_CMD="pytest -n 2 tests"
        if [ "${PX_CI_MINIMAL:-}" = "1" ]; then
            PYTEST_CMD="pytest -n 2 tests --ignore=tests/test_network.py"
        fi
        uv pip install tox tox-uv
        PXBIN="$PXBIN" uv run tox --installpkg "$PXWHEEL" \
            --override "tool.tox.env_run_base.install_command=uv pip install --no-index -f $WHEELS" \
            --override "tool.tox.env_run_base.commands=$PYTEST_CMD"
    fi
}
