"Large data transfer reliability tests for px proxy"

import asyncio
import hashlib
import multiprocessing
import os
import ssl
import threading
import time

import mcurl
import pytest
from helpers import find_free_port, quit_px, run_px

# Only run with: pytest -m largedata
pytestmark = pytest.mark.largedata

# ---------------------------------------------------------------------------
# Deterministic large payload generation
# ---------------------------------------------------------------------------
# Use a repeating pattern so both sender and receiver can independently
# compute the expected SHA-256 without transmitting the hash out of band.

PATTERN_BLOCK = (b"PxLargeDataTest" * 68)[:1024]  # exactly 1 KiB


def make_payload(size_bytes):
    """Build a deterministic bytes payload of exactly size_bytes."""
    full, remainder = divmod(size_bytes, len(PATTERN_BLOCK))
    return PATTERN_BLOCK * full + PATTERN_BLOCK[:remainder]


def payload_sha256(size_bytes):
    """Compute SHA-256 hex digest for the deterministic payload."""
    h = hashlib.sha256()
    full, remainder = divmod(size_bytes, len(PATTERN_BLOCK))
    for _ in range(full):
        h.update(PATTERN_BLOCK)
    if remainder:
        h.update(PATTERN_BLOCK[:remainder])
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Payload sizes (bytes)
# ---------------------------------------------------------------------------

SIZE_2MB = 2 * 1024 * 1024
SIZE_5MB = 5 * 1024 * 1024
SIZE_10MB = 10 * 1024 * 1024
SIZE_20MB = 20 * 1024 * 1024

# ---------------------------------------------------------------------------
# Async upstream server that serves / accepts large payloads
# ---------------------------------------------------------------------------


async def _read_request(reader):
    """Parse method, path, and content-length from an HTTP request."""
    request_line = await asyncio.wait_for(reader.readline(), timeout=30)
    if not request_line:
        return None, None, 0
    parts = request_line.decode("utf-8", errors="replace").split()
    if len(parts) < 2:
        return None, None, 0
    method, path = parts[0], parts[1]

    content_length = 0
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=30)
        if line in (b"\r\n", b"\n", b""):
            break
        lower = line.decode("utf-8", errors="replace").lower()
        if lower.startswith("content-length:"):
            content_length = int(lower.split(":", 1)[1].strip())
    return method, path, content_length


async def _handle_get(writer, path):
    """Respond with a deterministic large payload for GET /large/<size>."""
    try:
        size = int(path.split("/large/")[1])
    except (ValueError, IndexError):
        size = SIZE_2MB
    body = make_payload(size)
    digest = hashlib.sha256(body).hexdigest()
    header = (
        f"HTTP/1.1 200 OK\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"X-SHA256: {digest}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    writer.write(header + body)
    await writer.drain()


async def _handle_post(reader, writer, content_length):
    """Read posted body and respond with its SHA-256."""
    body = b""
    remaining = content_length
    while remaining > 0:
        chunk = await asyncio.wait_for(reader.read(min(remaining, 65536)), timeout=60)
        if not chunk:
            break
        body += chunk
        remaining -= len(chunk)
    digest = hashlib.sha256(body).hexdigest()
    resp_body = f'{{"received":{len(body)},"sha256":"{digest}"}}'.encode()
    header = (
        f"HTTP/1.1 200 OK\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(resp_body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    writer.write(header + resp_body)
    await writer.drain()


async def _handle_client(reader, writer):
    """Minimal HTTP server: GET returns large payload, POST echoes SHA-256."""
    try:
        method, path, content_length = await _read_request(reader)
        if method is None:
            return

        if method == "GET" and path.startswith("/large/"):
            await _handle_get(writer, path)
        elif method == "POST" and path.startswith("/upload"):
            await _handle_post(reader, writer, content_length)
        else:
            resp = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            writer.write(resp)
            await writer.drain()

    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


def _get_test_certs():
    """Return (certfile, keyfile) paths from pytest-httpbin's bundled certs."""
    certs_dir = os.path.join(os.path.dirname(__import__("pytest_httpbin").__file__), "certs")
    return os.path.join(certs_dir, "server.pem"), os.path.join(certs_dir, "server.key")


async def _run_server(http_port, https_port, certfile, keyfile, ready_event):
    http_srv = await asyncio.start_server(_handle_client, "127.0.0.1", http_port)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile, keyfile)
    https_srv = await asyncio.start_server(_handle_client, "127.0.0.1", https_port, ssl=ssl_ctx)

    ready_event.set()
    await asyncio.gather(http_srv.serve_forever(), https_srv.serve_forever())


def _server_process(http_port, https_port, certfile, keyfile, ready_event):
    asyncio.run(_run_server(http_port, https_port, certfile, keyfile, ready_event))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def large_upstream():
    """Start an async HTTP + HTTPS upstream server for large data tests."""
    http_port = find_free_port(4500)
    https_port = find_free_port(http_port + 1)
    assert http_port and https_port, "No free ports for large data upstream"

    certfile, keyfile = _get_test_certs()
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(
        target=_server_process,
        args=(http_port, https_port, certfile, keyfile, ready),
        daemon=True,
    )
    proc.start()
    assert ready.wait(timeout=10), "Large data upstream server didn't start"

    yield {"http_port": http_port, "https_port": https_port, "host": "127.0.0.1"}

    proc.kill()
    proc.join(timeout=5)


@pytest.fixture(scope="module")
def large_data_px(tmp_path_factory, large_upstream):
    """Start a Px instance for large data transfer tests."""
    port = find_free_port(4600)
    assert port is not None, "No free port for large data Px"
    name = "LargeData"
    flags = "--noproxy=127.0.0.1 --workers=1"
    subp, cmd, logfile = run_px(name, port, tmp_path_factory, flags)

    yield {
        "port": port,
        "subp": subp,
        "cmd": cmd,
        "logfile": logfile,
        "upstream": large_upstream,
    }

    quit_px(name, subp, cmd)


# ---------------------------------------------------------------------------
# Helper: make a request through the proxy
# ---------------------------------------------------------------------------


def _get_large(proxy_port, url, timeout=60):
    """GET a large payload through the proxy, return (data_bytes, headers_str, elapsed)."""
    start = time.monotonic()
    ec = mcurl.Curl(url, connect_timeout=timeout)
    ec.set_proxy("127.0.0.1", proxy_port)
    ec.set_insecure()
    ec.buffer()
    ret = ec.perform()
    elapsed = time.monotonic() - start
    if ret != 0:
        return None, "", elapsed
    data = ec.get_data(encoding=None)
    headers = ec.get_headers()
    return data, headers, elapsed


def _post_large(proxy_port, url, payload, timeout=60):
    """POST a large payload through the proxy, return (response_body, elapsed)."""
    start = time.monotonic()
    ec = mcurl.Curl(url, method="POST", connect_timeout=timeout)
    ec.set_proxy("127.0.0.1", proxy_port)
    ec.set_insecure()
    ec.buffer(payload)
    ec.set_headers({"Content-Length": len(payload)})
    ret = ec.perform()
    elapsed = time.monotonic() - start
    if ret != 0:
        return None, elapsed
    data = ec.get_data()
    return data, elapsed


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLargeGET:
    """Verify reliable large GET downloads through the proxy."""

    @pytest.mark.parametrize("size", [SIZE_2MB, SIZE_5MB, SIZE_10MB, SIZE_20MB], ids=["2MB", "5MB", "10MB", "20MB"])
    def test_http_large_get(self, large_data_px, size):
        """Single large HTTP GET — verify data integrity via SHA-256."""
        port = large_data_px["port"]
        up = large_data_px["upstream"]
        url = f"http://{up['host']}:{up['http_port']}/large/{size}"

        data, _headers, elapsed = _get_large(port, url)
        assert data is not None, "GET request failed"
        assert len(data) == size, f"Expected {size} bytes, got {len(data)}"

        actual = hashlib.sha256(data).hexdigest()
        expected = payload_sha256(size)
        assert actual == expected, f"SHA-256 mismatch: {actual} != {expected}"
        print(f"  HTTP GET {size / (1024 * 1024):.0f}MB: OK in {elapsed:.2f}s")

    @pytest.mark.parametrize("size", [SIZE_2MB, SIZE_5MB, SIZE_10MB, SIZE_20MB], ids=["2MB", "5MB", "10MB", "20MB"])
    def test_https_large_get(self, large_data_px, size):
        """Single large HTTPS GET (CONNECT tunnel) — verify data integrity."""
        port = large_data_px["port"]
        up = large_data_px["upstream"]
        url = f"https://{up['host']}:{up['https_port']}/large/{size}"

        data, _headers, elapsed = _get_large(port, url)
        assert data is not None, "GET request failed"
        assert len(data) == size, f"Expected {size} bytes, got {len(data)}"

        actual = hashlib.sha256(data).hexdigest()
        expected = payload_sha256(size)
        assert actual == expected, f"SHA-256 mismatch: {actual} != {expected}"
        print(f"  HTTPS GET {size / (1024 * 1024):.0f}MB: OK in {elapsed:.2f}s")

    def test_http_concurrent_large_get(self, large_data_px):
        """Multiple concurrent HTTP GET downloads — all must complete with correct data."""
        port = large_data_px["port"]
        up = large_data_px["upstream"]
        concurrency = 4
        size = SIZE_5MB
        url = f"http://{up['host']}:{up['http_port']}/large/{size}"
        expected_hash = payload_sha256(size)

        results = []
        lock = threading.Lock()

        def worker():
            data, _, elapsed = _get_large(port, url)
            with lock:
                results.append((data, elapsed))

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert len(results) == concurrency, f"Only {len(results)}/{concurrency} completed"
        for i, (data, elapsed) in enumerate(results):
            assert data is not None, f"Request {i} failed"
            assert len(data) == size, f"Request {i}: expected {size} bytes, got {len(data)}"
            actual = hashlib.sha256(data).hexdigest()
            assert actual == expected_hash, f"Request {i}: SHA-256 mismatch"
            print(f"  HTTP concurrent GET #{i}: {size / (1024 * 1024):.0f}MB in {elapsed:.2f}s")

    def test_https_concurrent_large_get(self, large_data_px):
        """Multiple concurrent HTTPS GET downloads through CONNECT tunnels."""
        port = large_data_px["port"]
        up = large_data_px["upstream"]
        concurrency = 4
        size = SIZE_5MB
        url = f"https://{up['host']}:{up['https_port']}/large/{size}"
        expected_hash = payload_sha256(size)

        results = []
        lock = threading.Lock()

        def worker():
            data, _, elapsed = _get_large(port, url)
            with lock:
                results.append((data, elapsed))

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert len(results) == concurrency, f"Only {len(results)}/{concurrency} completed"
        for i, (data, elapsed) in enumerate(results):
            assert data is not None, f"Request {i} failed"
            assert len(data) == size, f"Request {i}: expected {size} bytes, got {len(data)}"
            actual = hashlib.sha256(data).hexdigest()
            assert actual == expected_hash, f"Request {i}: SHA-256 mismatch"
            print(f"  HTTPS concurrent GET #{i}: {size / (1024 * 1024):.0f}MB in {elapsed:.2f}s")


class TestLargePOST:
    """Verify reliable large POST uploads through the proxy."""

    @pytest.mark.parametrize("size", [SIZE_2MB, SIZE_5MB, SIZE_10MB, SIZE_20MB], ids=["2MB", "5MB", "10MB", "20MB"])
    def test_http_large_post(self, large_data_px, size):
        """Single large HTTP POST — verify server received correct data."""
        port = large_data_px["port"]
        up = large_data_px["upstream"]
        url = f"http://{up['host']}:{up['http_port']}/upload"
        payload = make_payload(size)
        expected_hash = hashlib.sha256(payload).hexdigest()

        resp, elapsed = _post_large(port, url, payload)
        assert resp is not None, "POST request failed"
        assert f'"received":{size}' in resp, f"Server received wrong size: {resp}"
        assert expected_hash in resp, f"SHA-256 mismatch in response: {resp}"
        print(f"  HTTP POST {size / (1024 * 1024):.0f}MB: OK in {elapsed:.2f}s")

    @pytest.mark.parametrize("size", [SIZE_2MB, SIZE_5MB, SIZE_10MB, SIZE_20MB], ids=["2MB", "5MB", "10MB", "20MB"])
    def test_https_large_post(self, large_data_px, size):
        """Single large HTTPS POST through CONNECT tunnel."""
        port = large_data_px["port"]
        up = large_data_px["upstream"]
        url = f"https://{up['host']}:{up['https_port']}/upload"
        payload = make_payload(size)
        expected_hash = hashlib.sha256(payload).hexdigest()

        resp, elapsed = _post_large(port, url, payload)
        assert resp is not None, "POST request failed"
        assert f'"received":{size}' in resp, f"Server received wrong size: {resp}"
        assert expected_hash in resp, f"SHA-256 mismatch in response: {resp}"
        print(f"  HTTPS POST {size / (1024 * 1024):.0f}MB: OK in {elapsed:.2f}s")

    def test_http_concurrent_large_post(self, large_data_px):
        """Multiple concurrent HTTP POST uploads — all must succeed with correct hashes."""
        port = large_data_px["port"]
        up = large_data_px["upstream"]
        concurrency = 4
        size = SIZE_5MB
        url = f"http://{up['host']}:{up['http_port']}/upload"
        payload = make_payload(size)
        expected_hash = hashlib.sha256(payload).hexdigest()

        results = []
        lock = threading.Lock()

        def worker():
            resp, elapsed = _post_large(port, url, payload)
            with lock:
                results.append((resp, elapsed))

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert len(results) == concurrency, f"Only {len(results)}/{concurrency} completed"
        for i, (resp, elapsed) in enumerate(results):
            assert resp is not None, f"Request {i} failed"
            assert f'"received":{size}' in resp, f"Request {i}: wrong size: {resp}"
            assert expected_hash in resp, f"Request {i}: SHA-256 mismatch: {resp}"
            print(f"  HTTP concurrent POST #{i}: {size / (1024 * 1024):.0f}MB in {elapsed:.2f}s")

    def test_https_concurrent_large_post(self, large_data_px):
        """Multiple concurrent HTTPS POST uploads through CONNECT tunnels."""
        port = large_data_px["port"]
        up = large_data_px["upstream"]
        concurrency = 4
        size = SIZE_5MB
        url = f"https://{up['host']}:{up['https_port']}/upload"
        payload = make_payload(size)
        expected_hash = hashlib.sha256(payload).hexdigest()

        results = []
        lock = threading.Lock()

        def worker():
            resp, elapsed = _post_large(port, url, payload)
            with lock:
                results.append((resp, elapsed))

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert len(results) == concurrency, f"Only {len(results)}/{concurrency} completed"
        for i, (resp, elapsed) in enumerate(results):
            assert resp is not None, f"Request {i} failed"
            assert f'"received":{size}' in resp, f"Request {i}: wrong size: {resp}"
            assert expected_hash in resp, f"Request {i}: SHA-256 mismatch: {resp}"
            print(f"  HTTPS concurrent POST #{i}: {size / (1024 * 1024):.0f}MB in {elapsed:.2f}s")


class TestMixedConcurrent:
    """Verify reliability when GET and POST transfers happen simultaneously."""

    def test_mixed_http_concurrent(self, large_data_px):
        """Concurrent HTTP GETs and POSTs simultaneously."""
        port = large_data_px["port"]
        up = large_data_px["upstream"]
        size = SIZE_5MB
        get_url = f"http://{up['host']}:{up['http_port']}/large/{size}"
        post_url = f"http://{up['host']}:{up['http_port']}/upload"
        payload = make_payload(size)
        expected_get_hash = payload_sha256(size)
        expected_post_hash = hashlib.sha256(payload).hexdigest()

        get_results = []
        post_results = []
        lock = threading.Lock()

        def get_worker():
            data, _, elapsed = _get_large(port, get_url)
            with lock:
                get_results.append((data, elapsed))

        def post_worker():
            resp, elapsed = _post_large(port, post_url, payload)
            with lock:
                post_results.append((resp, elapsed))

        threads = []
        for _ in range(3):
            threads.append(threading.Thread(target=get_worker))
            threads.append(threading.Thread(target=post_worker))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert len(get_results) == 3, f"Only {len(get_results)}/3 GETs completed"
        assert len(post_results) == 3, f"Only {len(post_results)}/3 POSTs completed"

        for i, (data, _elapsed) in enumerate(get_results):
            assert data is not None, f"GET {i} failed"
            assert len(data) == size, f"GET {i}: expected {size} bytes, got {len(data)}"
            actual = hashlib.sha256(data).hexdigest()
            assert actual == expected_get_hash, f"GET {i}: SHA-256 mismatch"

        for i, (resp, _elapsed) in enumerate(post_results):
            assert resp is not None, f"POST {i} failed"
            assert f'"received":{size}' in resp, f"POST {i}: wrong size"
            assert expected_post_hash in resp, f"POST {i}: SHA-256 mismatch"

        print(f"  Mixed HTTP: 3 GETs + 3 POSTs of {size / (1024 * 1024):.0f}MB all OK")

    def test_mixed_https_concurrent(self, large_data_px):
        """Concurrent HTTPS GETs and POSTs through CONNECT tunnels simultaneously."""
        port = large_data_px["port"]
        up = large_data_px["upstream"]
        size = SIZE_5MB
        get_url = f"https://{up['host']}:{up['https_port']}/large/{size}"
        post_url = f"https://{up['host']}:{up['https_port']}/upload"
        payload = make_payload(size)
        expected_get_hash = payload_sha256(size)
        expected_post_hash = hashlib.sha256(payload).hexdigest()

        get_results = []
        post_results = []
        lock = threading.Lock()

        def get_worker():
            data, _, elapsed = _get_large(port, get_url)
            with lock:
                get_results.append((data, elapsed))

        def post_worker():
            resp, elapsed = _post_large(port, post_url, payload)
            with lock:
                post_results.append((resp, elapsed))

        threads = []
        for _ in range(3):
            threads.append(threading.Thread(target=get_worker))
            threads.append(threading.Thread(target=post_worker))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert len(get_results) == 3, f"Only {len(get_results)}/3 GETs completed"
        assert len(post_results) == 3, f"Only {len(post_results)}/3 POSTs completed"

        for i, (data, _elapsed) in enumerate(get_results):
            assert data is not None, f"GET {i} failed"
            assert len(data) == size, f"GET {i}: expected {size} bytes, got {len(data)}"
            actual = hashlib.sha256(data).hexdigest()
            assert actual == expected_get_hash, f"GET {i}: SHA-256 mismatch"

        for i, (resp, _elapsed) in enumerate(post_results):
            assert resp is not None, f"POST {i} failed"
            assert f'"received":{size}' in resp, f"POST {i}: wrong size"
            assert expected_post_hash in resp, f"POST {i}: SHA-256 mismatch"

        print(f"  Mixed HTTPS: 3 GETs + 3 POSTs of {size / (1024 * 1024):.0f}MB all OK")
