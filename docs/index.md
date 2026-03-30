# Px — Documentation

This folder contains the documentation for the `px-proxy` Python package — an
HTTP proxy server that automatically authenticates through NTLM or Kerberos
proxy servers using Windows SSPI or configured credentials.

## User documentation

| File | Description |
|------|-------------|
| [installation.md](installation.md) | Install via pip, wheels, binary, Docker, Winget, Scoop; uninstallation |
| [usage.md](usage.md) | Credentials, client auth, examples, dependencies, limitations |
| [configuration.md](configuration.md) | All CLI flags, environment variables, INI keys, auth types |

## Developer documentation

| File | Description |
|------|-------------|
| [architecture.md](architecture.md) | Runtime model, package layout, data flow, state management |
| [build.md](build.md) | Build system: `pyproject.toml`, GitHub Actions, wheels, Nuitka, Docker |
| [testing.md](testing.md) | Test suite layout, running tests, fixtures, coverage |
| [changelog.md](changelog.md) | Release history |

## Quick reference

- **PyPI name**: `px-proxy`
- **Requires**: Python ≥ 3.10
- **Key dependencies**: [pymcurl](https://pypi.org/project/pymcurl/), [keyring](https://pypi.org/project/keyring/), [keyrings.alt](https://pypi.org/project/keyrings.alt/), [netaddr](https://pypi.org/project/netaddr/), [psutil](https://pypi.org/project/psutil/), [pyspnego](https://pypi.org/project/pyspnego/), [quickjs-ng](https://pypi.org/project/quickjs-ng/), [python-dotenv](https://pypi.org/project/python-dotenv/)
