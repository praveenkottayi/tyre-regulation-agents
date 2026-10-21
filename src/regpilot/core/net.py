"""
HTTP helper. GET only, with bounded retries: GETs are idempotent, so retrying
them is safe. (Graph nodes themselves are NOT auto-retried; see watcher/graph.py.)
"""

import time

import requests

try:  # use the OS certificate store (needed behind corporate TLS-inspecting proxies)
    import truststore

    truststore.inject_into_ssl()
except Exception:  # pragma: no cover
    pass

USER_AGENT = "regpilot/0.2 (tyre regulation watcher)"


class FetchError(RuntimeError):
    pass


def get(url: str, *, headers: dict | None = None, params: dict | None = None,
        timeout: int = 120, retries: int = 3) -> requests.Response:
    h = {"User-Agent": USER_AGENT, **(headers or {})}
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                last = FetchError(f"HTTP {r.status_code} from {url}")
            else:
                return r
        except requests.RequestException as e:
            last = FetchError(f"{type(e).__name__}: {e}")
        time.sleep(2 ** attempt)
    raise last  # type: ignore[misc]
