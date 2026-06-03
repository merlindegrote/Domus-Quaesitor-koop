"""Subprocess helper for curl_cffi requests with reliable timeout kill."""
import json
import random
import sys
from curl_cffi import requests as curl_requests

_IMPERSONATE_BROWSERS = ["chrome124", "chrome123", "safari17_0"]

if __name__ == "__main__":
    url = sys.argv[1]
    timeout = int(sys.argv[2])
    headers = json.loads(sys.argv[3])
    kwargs_str = sys.argv[4] if len(sys.argv) > 4 else "{}"
    kwargs = json.loads(kwargs_str)

    try:
        browser = random.choice(_IMPERSONATE_BROWSERS)
        resp = curl_requests.get(
            url, impersonate=browser, timeout=timeout,
            headers=headers, **kwargs,
        )
        resp.raise_for_status()
        out = {"ok": True, "text": resp.text}
        print(json.dumps(out), flush=True)
    except Exception as e:
        out = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:200]}"}
        print(json.dumps(out), flush=True)
