"Concurrency benchmark tests for px async server"

import asyncio
import contextlib
import multiprocessing
import os
import ssl
import threading
import time

import mcurl
import psutil
import pytest
from helpers import find_free_port, quit_px, run_px

# Only run with: pytest -m benchmark
pytestmark = pytest.mark.benchmark

# ---------------------------------------------------------------------------
# Fast async upstream server (HTTP + HTTPS) for benchmarking
# ---------------------------------------------------------------------------
# Pure-asyncio server that returns minimal responses instantly.  Unlike
# single-threaded httpbin/werkzeug, this saturates well past 1 000 concurrent
# connections so the proxy is always the bottleneck being measured.

_RESPONSE_BODY = b'{"status":"ok","data":"' + b"x" * 200 + b'"}\n'
_HTTP_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(_RESPONSE_BODY)).encode() + b"\r\n"
    b"Connection: close\r\n\r\n" + _RESPONSE_BODY
)


async def _handle_fast_client(reader, writer):
    """Read one HTTP request, send a canned 200 response, close."""
    try:
        # Read until blank line (end of headers)
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line in (b"\r\n", b"\n", b""):
                break
        writer.write(_HTTP_RESPONSE)
        await writer.drain()
    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


def _get_test_certs():
    """Return (certfile, keyfile) paths from pytest-httpbin's bundled certs."""
    certs_dir = os.path.join(os.path.dirname(__import__("pytest_httpbin").__file__), "certs")
    return os.path.join(certs_dir, "server.pem"), os.path.join(certs_dir, "server.key")


async def _run_fast_server(http_port, https_port, certfile, keyfile, ready_event):
    """Start async HTTP and HTTPS servers, set ready_event when listening."""
    http_srv = await asyncio.start_server(_handle_fast_client, "127.0.0.1", http_port)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile, keyfile)
    https_srv = await asyncio.start_server(_handle_fast_client, "127.0.0.1", https_port, ssl=ssl_ctx)

    ready_event.set()
    await asyncio.gather(http_srv.serve_forever(), https_srv.serve_forever())


def _fast_server_process(http_port, https_port, certfile, keyfile, ready_event):
    asyncio.run(_run_fast_server(http_port, https_port, certfile, keyfile, ready_event))


@pytest.fixture(scope="module")
def fast_upstream():
    """Start a fast async HTTP + HTTPS upstream server in a separate process."""
    http_port = find_free_port(4300)
    https_port = find_free_port(http_port + 1)
    assert http_port and https_port, "No free ports"

    certfile, keyfile = _get_test_certs()
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_fast_server_process,
        args=(http_port, https_port, certfile, keyfile, ready),
        daemon=True,
    )
    proc.start()
    assert ready.wait(timeout=10), "Fast upstream server didn't start"

    yield {"http_port": http_port, "https_port": https_port, "host": "127.0.0.1"}

    proc.kill()
    proc.join(timeout=5)


@pytest.fixture(scope="module")
def benchmark_px(tmp_path_factory, fast_upstream):
    """Start a Px instance configured with noproxy (direct to upstream)."""
    port = find_free_port(4200)
    assert port is not None, "No free port found"
    name = "Benchmark"
    flags = "--noproxy=127.0.0.1 --workers=1"
    subp, cmd, logfile = run_px(name, port, tmp_path_factory, flags)

    yield {
        "port": port,
        "subp": subp,
        "cmd": cmd,
        "logfile": logfile,
        "pid": subp.pid,
        "upstream": fast_upstream,
    }

    quit_px(name, subp, cmd, strict=False)


def _make_http_request(proxy_port, target_url, timeout=10):
    """Make a single HTTP GET request through the proxy using mcurl, return (success, elapsed)."""
    start = time.monotonic()
    try:
        ec = mcurl.Curl(target_url, connect_timeout=timeout)
        ec.set_proxy("127.0.0.1", proxy_port)
        ec.buffer()
        ret = ec.perform()
        elapsed = time.monotonic() - start
        _, status = ec.get_response()
        success = ret == 0 and 200 <= status < 400
    except Exception:
        return False, time.monotonic() - start
    else:
        return success, elapsed


def _make_connect_request(proxy_port, target_host, target_port, timeout=10):
    """Make a CONNECT tunnel request through the proxy using mcurl, return (success, elapsed).

    Requesting HTTPS through an HTTP proxy triggers a CONNECT tunnel automatically."""
    start = time.monotonic()
    try:
        url = f"https://{target_host}:{target_port}/get"
        ec = mcurl.Curl(url, connect_timeout=timeout)
        ec.set_proxy("127.0.0.1", proxy_port)
        ec.set_insecure()
        ec.buffer()
        ret = ec.perform()
        elapsed = time.monotonic() - start
        _, status = ec.get_response()
        success = ret == 0 and 200 <= status < 400
    except Exception:
        return False, time.monotonic() - start
    else:
        return success, elapsed


def _get_process_stats(pid):
    """Get thread count and RSS memory for a process and its children."""
    try:
        proc = psutil.Process(pid)
        children = proc.children(recursive=True)
        all_procs = [proc, *children]

        threads = sum(p.num_threads() for p in all_procs)
        rss = sum(p.memory_info().rss for p in all_procs)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0, 0
    else:
        return threads, rss


def _run_concurrent_requests(func, concurrency, total_requests, *args):
    """Run total_requests using a thread pool of size concurrency, return results."""
    results = []
    lock = threading.Lock()

    def worker():
        result = func(*args)
        with lock:
            results.append(result)

    threads = []
    for _ in range(total_requests):
        while len(threads) - sum(1 for t in threads if not t.is_alive()) >= concurrency:
            time.sleep(0.001)
        t = threading.Thread(target=worker)
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=60)

    return results


class TestHTTPBenchmark:
    """Benchmark HTTP GET throughput at various concurrency levels."""

    CONCURRENCY_LEVELS = (1, 10, 25, 50, 100, 200)
    REQUESTS_PER_LEVEL = 100

    def test_http_throughput(self, benchmark_px):
        """Measure HTTP GET throughput and resource usage at different concurrency levels."""
        port = benchmark_px["port"]
        up = benchmark_px["upstream"]
        target_url = f"http://{up['host']}:{up['http_port']}/get"
        pid = benchmark_px["pid"]

        print("\n--- HTTP GET Benchmark ---")
        print(
            f"{'Concurrency':>12} {'Succeeded':>10} {'Failed':>8} {'Avg(ms)':>10} "
            f"{'p50(ms)':>10} {'p99(ms)':>10} {'Req/s':>8} {'Threads':>8} {'RSS(MB)':>10}"
        )

        for conc in self.CONCURRENCY_LEVELS:
            # Warm up
            _make_http_request(port, target_url)

            # Measure
            threads_before, rss_before = _get_process_stats(pid)
            start = time.monotonic()
            results = _run_concurrent_requests(_make_http_request, conc, self.REQUESTS_PER_LEVEL, port, target_url)
            wall_time = time.monotonic() - start
            threads_after, rss_after = _get_process_stats(pid)

            successes = sum(1 for s, _ in results if s)
            failures = len(results) - successes
            latencies = sorted(e for s, e in results if s)

            if latencies:
                avg_ms = sum(latencies) / len(latencies) * 1000
                p50_ms = latencies[len(latencies) // 2] * 1000
                p99_ms = latencies[int(len(latencies) * 0.99)] * 1000
                rps = successes / wall_time
            else:
                avg_ms = p50_ms = p99_ms = rps = 0

            threads_peak = max(threads_before, threads_after)
            rss_mb = max(rss_before, rss_after) / (1024 * 1024)

            print(
                f"{conc:>12} {successes:>10} {failures:>8} {avg_ms:>10.1f} "
                f"{p50_ms:>10.1f} {p99_ms:>10.1f} {rps:>8.1f} {threads_peak:>8} {rss_mb:>10.1f}"
            )

            # At least 80% of requests should succeed
            assert successes >= self.REQUESTS_PER_LEVEL * 0.8, (
                f"Too many failures at concurrency {conc}: {failures}/{self.REQUESTS_PER_LEVEL}"
            )


class TestCONNECTBenchmark:
    """Benchmark CONNECT tunnel throughput at various concurrency levels."""

    CONCURRENCY_LEVELS = (1, 10, 25, 50, 100, 200)
    REQUESTS_PER_LEVEL = 100

    def test_connect_throughput(self, benchmark_px):
        """Measure CONNECT tunnel throughput and resource usage at different concurrency levels."""
        port = benchmark_px["port"]
        up = benchmark_px["upstream"]
        target_host = up["host"]
        target_port = up["https_port"]
        pid = benchmark_px["pid"]

        print("\n--- CONNECT Tunnel Benchmark ---")
        print(
            f"{'Concurrency':>12} {'Succeeded':>10} {'Failed':>8} {'Avg(ms)':>10} "
            f"{'p50(ms)':>10} {'p99(ms)':>10} {'Req/s':>8} {'Threads':>8} {'RSS(MB)':>10}"
        )

        for conc in self.CONCURRENCY_LEVELS:
            # Warm up
            _make_connect_request(port, target_host, target_port)

            # Measure
            threads_before, rss_before = _get_process_stats(pid)
            start = time.monotonic()
            results = _run_concurrent_requests(
                _make_connect_request, conc, self.REQUESTS_PER_LEVEL, port, target_host, target_port
            )
            wall_time = time.monotonic() - start
            threads_after, rss_after = _get_process_stats(pid)

            successes = sum(1 for s, _ in results if s)
            failures = len(results) - successes
            latencies = sorted(e for s, e in results if s)

            if latencies:
                avg_ms = sum(latencies) / len(latencies) * 1000
                p50_ms = latencies[len(latencies) // 2] * 1000
                p99_ms = latencies[int(len(latencies) * 0.99)] * 1000
                rps = successes / wall_time
            else:
                avg_ms = p50_ms = p99_ms = rps = 0

            threads_peak = max(threads_before, threads_after)
            rss_mb = max(rss_before, rss_after) / (1024 * 1024)

            print(
                f"{conc:>12} {successes:>10} {failures:>8} {avg_ms:>10.1f} "
                f"{p50_ms:>10.1f} {p99_ms:>10.1f} {rps:>8.1f} {threads_peak:>8} {rss_mb:>10.1f}"
            )

            # At least 60% of CONNECT requests should succeed (tunnels are harder)
            assert successes >= self.REQUESTS_PER_LEVEL * 0.6, (
                f"Too many failures at concurrency {conc}: {failures}/{self.REQUESTS_PER_LEVEL}"
            )


class TestResourceUsage:
    """Verify that the async server keeps resource usage bounded under load."""

    def test_thread_count_bounded(self, benchmark_px):
        """Thread count should stay roughly constant regardless of concurrent connections."""
        port = benchmark_px["port"]
        up = benchmark_px["upstream"]
        target_url = f"http://{up['host']}:{up['http_port']}/get"
        pid = benchmark_px["pid"]

        # Baseline thread count
        _make_http_request(port, target_url)
        time.sleep(0.5)
        baseline_threads, _ = _get_process_stats(pid)

        # Hammer with 50 concurrent requests
        _run_concurrent_requests(_make_http_request, 50, 100, port, target_url)

        # Check thread count during/after load
        loaded_threads, _ = _get_process_stats(pid)

        print("\n--- Thread Count ---")
        print(f"Baseline: {baseline_threads}, Under load: {loaded_threads}")

        # Async server should not spawn threads proportional to connections
        # Allow some headroom but not 50+ new threads
        assert loaded_threads < baseline_threads + 30, (
            f"Thread count grew too much: {baseline_threads} -> {loaded_threads}"
        )

    def test_memory_bounded(self, benchmark_px):
        """Memory should not grow excessively under concurrent load."""
        port = benchmark_px["port"]
        up = benchmark_px["upstream"]
        target_url = f"http://{up['host']}:{up['http_port']}/get"
        pid = benchmark_px["pid"]

        # Baseline memory
        _make_http_request(port, target_url)
        time.sleep(0.5)
        _, baseline_rss = _get_process_stats(pid)

        # Heavy load
        _run_concurrent_requests(_make_http_request, 50, 200, port, target_url)

        _, loaded_rss = _get_process_stats(pid)

        baseline_mb = baseline_rss / (1024 * 1024)
        loaded_mb = loaded_rss / (1024 * 1024)

        print("\n--- Memory Usage ---")
        print(f"Baseline: {baseline_mb:.1f} MB, After load: {loaded_mb:.1f} MB")

        # Memory should not more than double under load
        assert loaded_rss < baseline_rss * 2 + 50 * 1024 * 1024, (
            f"Memory grew too much: {baseline_mb:.1f} MB -> {loaded_mb:.1f} MB"
        )


class TestThreadSaturation:
    """Find the concurrency level where the thread pool becomes the bottleneck.

    Each HTTP request holds a thread pool slot for the duration of mcurl.do().
    This test escalates concurrency well past the pool size to observe queuing,
    latency degradation, and throughput plateau.
    """

    CONCURRENCY_LEVELS = (16, 32, 64, 96, 128, 192, 256, 384, 512)
    REQUESTS_PER_LEVEL = 200

    def test_thread_saturation(self, benchmark_px):
        """Escalate concurrency past the thread pool to find the saturation point."""
        port = benchmark_px["port"]
        up = benchmark_px["upstream"]
        target_url = f"http://{up['host']}:{up['http_port']}/get"
        pid = benchmark_px["pid"]

        print("\n--- Thread Pool Saturation Benchmark ---")
        print(
            f"{'Concurrency':>12} {'Succeeded':>10} {'Failed':>8} {'Avg(ms)':>10} "
            f"{'p50(ms)':>10} {'p99(ms)':>10} {'Req/s':>8} {'Threads':>8} {'RSS(MB)':>10}"
        )

        prev_rps = 0
        saturation_conc = None

        for conc in self.CONCURRENCY_LEVELS:
            # Warm up
            _make_http_request(port, target_url)

            # Sample threads during the run with a background sampler
            peak_threads = [0]
            stop_sampling = threading.Event()

            def sample_threads(stop=stop_sampling, peak=peak_threads):
                while not stop.is_set():
                    t, _ = _get_process_stats(pid)
                    if t > peak[0]:
                        peak[0] = t
                    time.sleep(0.05)

            sampler = threading.Thread(target=sample_threads, daemon=True)
            sampler.start()

            start = time.monotonic()
            results = _run_concurrent_requests(_make_http_request, conc, self.REQUESTS_PER_LEVEL, port, target_url)
            wall_time = time.monotonic() - start

            stop_sampling.set()
            sampler.join(timeout=2)

            _, rss = _get_process_stats(pid)

            successes = sum(1 for s, _ in results if s)
            failures = len(results) - successes
            latencies = sorted(e for s, e in results if s)

            if latencies:
                avg_ms = sum(latencies) / len(latencies) * 1000
                p50_ms = latencies[len(latencies) // 2] * 1000
                p99_ms = latencies[int(len(latencies) * 0.99)] * 1000
                rps = successes / wall_time
            else:
                avg_ms = p50_ms = p99_ms = rps = 0

            rss_mb = rss / (1024 * 1024)

            print(
                f"{conc:>12} {successes:>10} {failures:>8} {avg_ms:>10.1f} "
                f"{p50_ms:>10.1f} {p99_ms:>10.1f} {rps:>8.1f} "
                f"{peak_threads[0]:>8} {rss_mb:>10.1f}"
            )

            # Detect saturation: throughput stops growing relative to concurrency
            if prev_rps > 0 and rps < prev_rps * 1.1 and saturation_conc is None:
                saturation_conc = conc

            prev_rps = rps

            # All requests should eventually complete
            assert successes >= self.REQUESTS_PER_LEVEL * 0.7, (
                f"Too many failures at concurrency {conc}: {failures}/{self.REQUESTS_PER_LEVEL}"
            )

        if saturation_conc:
            print(f"\nThread pool saturation detected around concurrency={saturation_conc}")
        else:
            print("\nNo clear saturation point detected in tested range")


class TestActiveDataExchange:
    """Benchmark parallel CONNECT tunnels that actively exchange data.

    Unlike the basic CONNECT benchmark that establishes a tunnel, does one
    small GET, and tears down, this test holds tunnels open while data flows
    through them. All tunnels are open simultaneously, stressing the
    TunnelRelay FD-watcher multiplexer.
    """

    CONCURRENCY_LEVELS = (4, 16, 32, 64, 128, 256, 512)

    @staticmethod
    def _tunnel_with_data(proxy_port, target_host, target_port, timeout=30):
        """Open a CONNECT tunnel via mcurl, do a GET /get, and return stats.

        Returns (success, bytes_received, elapsed_seconds).
        """
        start = time.monotonic()
        try:
            url = f"https://{target_host}:{target_port}/get"
            ec = mcurl.Curl(url, connect_timeout=timeout)
            ec.set_proxy("127.0.0.1", proxy_port)
            ec.set_insecure()
            ec.buffer()
            ret = ec.perform()
            elapsed = time.monotonic() - start
            _, status = ec.get_response()
            if ret == 0 and 200 <= status < 400:
                data = ec.get_data(encoding=None)
                return True, len(data), elapsed
            else:
                return False, 0, elapsed
        except Exception:
            return False, 0, time.monotonic() - start

    def test_active_tunnels(self, benchmark_px):
        """Many parallel tunnels all actively exchanging data at the same time.

        All tunnels launch simultaneously via a barrier so they overlap
        maximally, exercising the event loop's FD watcher multiplexing.
        """
        port = benchmark_px["port"]
        up = benchmark_px["upstream"]
        target_host = up["host"]
        target_port = up["https_port"]
        pid = benchmark_px["pid"]

        print("\n--- Active Data Exchange Benchmark (concurrent CONNECT + GET) ---")
        print(
            f"{'Tunnels':>12} {'Succeeded':>10} {'Failed':>8} "
            f"{'Total(KB)':>10} {'Avg(ms)':>10} "
            f"{'p50(ms)':>10} {'p99(ms)':>10} {'Threads':>8} {'RSS(MB)':>10}"
        )

        for conc in self.CONCURRENCY_LEVELS:
            # Warm up
            self._tunnel_with_data(port, target_host, target_port)

            # Sample peak threads during the run
            peak_threads = [0]
            stop_sampling = threading.Event()

            def sample_threads(stop=stop_sampling, peak=peak_threads):
                while not stop.is_set():
                    t, _ = _get_process_stats(pid)
                    if t > peak[0]:
                        peak[0] = t
                    time.sleep(0.05)

            sampler = threading.Thread(target=sample_threads, daemon=True)
            sampler.start()

            # Use a barrier so all tunnels start at the same time
            barrier = threading.Barrier(conc, timeout=10)
            results = []
            lock = threading.Lock()

            def worker(b=barrier, r=results, lk=lock):
                with contextlib.suppress(threading.BrokenBarrierError):
                    b.wait()
                result = self._tunnel_with_data(port, target_host, target_port)
                with lk:
                    r.append(result)

            threads = []
            for _ in range(conc):
                t = threading.Thread(target=worker)
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=60)

            stop_sampling.set()
            sampler.join(timeout=2)

            _, rss = _get_process_stats(pid)

            successes = sum(1 for s, _, _ in results if s)
            failures = len(results) - successes
            total_bytes = sum(b for _, b, _ in results)
            total_kb = total_bytes / 1024

            latencies = sorted(e for s, _, e in results if s)
            if latencies:
                avg_ms = sum(latencies) / len(latencies) * 1000
                p50_ms = latencies[len(latencies) // 2] * 1000
                p99_ms = latencies[int(len(latencies) * 0.99)] * 1000
            else:
                avg_ms = p50_ms = p99_ms = 0

            rss_mb = rss / (1024 * 1024)

            print(
                f"{conc:>12} {successes:>10} {failures:>8} "
                f"{total_kb:>10.1f} {avg_ms:>10.1f} "
                f"{p50_ms:>10.1f} {p99_ms:>10.1f} "
                f"{peak_threads[0]:>8} {rss_mb:>10.1f}"
            )

            # At least 60% should succeed
            assert successes >= conc * 0.6, f"Too many failures at {conc} tunnels: {failures}/{conc}"
