"""Plain HTTP JSON helpers shared by every outbound data source."""
import json
import time
import urllib.request

import requests as _requests


def _http_get_json(url: str, timeout: int = 10) -> dict | None:
    """Shared HTTP GET → JSON. Returns None on any failure; used by shopping.py and traffic.py."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Palmer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"HTTP GET failed ({url.split('?')[0]}): {type(e).__name__}: {e}")
        return None

def _http_get_json_retry(url: str, params: dict, timeout: float, attempts: int = 3,
                         headers: dict | None = None) -> dict:
    """GET with retries and backoff — Open-Meteo's free tier rate-limits shared
    Heroku IPs (429s) and transient timeouts are common, so one bare request
    fails far too often. Raises on final failure (unlike _http_get_json, which returns None)."""
    last_err = None
    for i in range(attempts):
        try:
            resp = _requests.get(url, params=params, timeout=timeout, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    raise last_err
