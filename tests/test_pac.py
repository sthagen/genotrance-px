"""Tests for px.pac module — PAC file loading and evaluation."""

from px.pac import Pac

SIMPLE_PAC = b"""
function FindProxyForURL(url, host) {
    if (host == "direct.example.com") {
        return "DIRECT";
    }
    if (host == "proxy.example.com") {
        return "PROXY proxy1.com:8080";
    }
    if (host == "multi.example.com") {
        return "PROXY proxy1.com:8080; PROXY proxy2.com:3128; DIRECT";
    }
    if (host == "socks.example.com") {
        return "SOCKS5 socks.com:1080";
    }
    return "DIRECT";
}
"""

BROKEN_PAC = b"""
this is not valid javascript {{{{
"""


class TestPacLoad:
    def test_load_from_file(self, tmp_path):
        pac_file = tmp_path / "proxy.pac"
        pac_file.write_bytes(SIMPLE_PAC)
        pac = Pac(str(pac_file))
        result = pac.find_proxy_for_url("http://direct.example.com", "direct.example.com")
        assert "DIRECT" in result

    def test_load_returns_direct_for_matching_host(self, tmp_path):
        pac_file = tmp_path / "proxy.pac"
        pac_file.write_bytes(SIMPLE_PAC)
        pac = Pac(str(pac_file))
        result = pac.find_proxy_for_url("http://direct.example.com", "direct.example.com")
        assert result == "DIRECT"

    def test_load_returns_proxy(self, tmp_path):
        pac_file = tmp_path / "proxy.pac"
        pac_file.write_bytes(SIMPLE_PAC)
        pac = Pac(str(pac_file))
        result = pac.find_proxy_for_url("http://proxy.example.com", "proxy.example.com")
        assert "proxy1.com:8080" in result
        # PROXY prefix should be stripped
        assert "PROXY " not in result

    def test_multiple_proxies_with_direct(self, tmp_path):
        pac_file = tmp_path / "proxy.pac"
        pac_file.write_bytes(SIMPLE_PAC)
        pac = Pac(str(pac_file))
        result = pac.find_proxy_for_url("http://multi.example.com", "multi.example.com")
        assert "proxy1.com:8080" in result
        assert "proxy2.com:3128" in result
        assert "DIRECT" in result
        # Semicolons should be converted to commas
        assert ";" not in result

    def test_socks5_proxy(self, tmp_path):
        pac_file = tmp_path / "proxy.pac"
        pac_file.write_bytes(SIMPLE_PAC)
        pac = Pac(str(pac_file))
        result = pac.find_proxy_for_url("http://socks.example.com", "socks.example.com")
        assert "socks5://socks.com:1080" in result

    def test_unknown_host_returns_direct(self, tmp_path):
        pac_file = tmp_path / "proxy.pac"
        pac_file.write_bytes(SIMPLE_PAC)
        pac = Pac(str(pac_file))
        result = pac.find_proxy_for_url("http://unknown.example.com", "unknown.example.com")
        assert result == "DIRECT"

    def test_broken_pac_returns_direct(self, tmp_path):
        pac_file = tmp_path / "broken.pac"
        pac_file.write_bytes(BROKEN_PAC)
        pac = Pac(str(pac_file))
        result = pac.find_proxy_for_url("http://example.com", "example.com")
        assert result == "DIRECT"

    def test_encoding_latin1(self, tmp_path):
        pac_content = 'function FindProxyForURL(url, host) { return "DIRECT"; }'.encode("latin-1")
        pac_file = tmp_path / "latin.pac"
        pac_file.write_bytes(pac_content)
        pac = Pac(str(pac_file), pac_encoding="latin-1")
        result = pac.find_proxy_for_url("http://example.com", "example.com")
        assert result == "DIRECT"

    def test_wrong_encoding_returns_direct(self, tmp_path):
        # UTF-16 content but declared as UTF-8
        pac_content = 'function FindProxyForURL(url, host) { return "PROXY p:80"; }'.encode("utf-16")
        pac_file = tmp_path / "bad_enc.pac"
        pac_file.write_bytes(pac_content)
        pac = Pac(str(pac_file), pac_encoding="utf-8")
        result = pac.find_proxy_for_url("http://example.com", "example.com")
        assert result == "DIRECT"


class TestPacCleanup:
    def test_del_releases_resources(self, tmp_path):
        pac_file = tmp_path / "proxy.pac"
        pac_file.write_bytes(SIMPLE_PAC)
        pac = Pac(str(pac_file))
        # Force load
        pac.find_proxy_for_url("http://example.com", "example.com")
        assert pac.pac_find_proxy_for_url is not None
        pac.__del__()
        assert pac.pac_find_proxy_for_url is None

    def test_del_safe_when_not_loaded(self):
        pac = Pac("/nonexistent")
        # Should not raise
        pac.__del__()


class TestPacCallables:
    def test_dns_resolve(self, tmp_path):
        pac_file = tmp_path / "proxy.pac"
        pac_file.write_bytes(SIMPLE_PAC)
        pac = Pac(str(pac_file))
        result = pac.dnsResolve("localhost")
        assert result == "127.0.0.1"

    def test_dns_resolve_bad_host(self, tmp_path):
        pac_file = tmp_path / "proxy.pac"
        pac_file.write_bytes(SIMPLE_PAC)
        pac = Pac(str(pac_file))
        result = pac.dnsResolve("this.host.definitely.does.not.exist.invalid")
        assert result == ""

    def test_my_ip_address(self, tmp_path):
        pac_file = tmp_path / "proxy.pac"
        pac_file.write_bytes(SIMPLE_PAC)
        pac = Pac(str(pac_file))
        result = pac.myIpAddress()
        # Should return some IP address
        assert len(result) > 0
