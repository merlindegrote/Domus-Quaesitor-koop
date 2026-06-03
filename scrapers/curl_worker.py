"""Persistent subprocess worker for curl_cffi requests.
Reads URLs from stdin, writes results to temp files (to avoid pipe buffer limits)."""
import json
import os
import random
import sys
import tempfile
from curl_cffi import requests as curl_requests

_IMPERSONATE_BROWSERS = ["chrome124", "chrome123", "safari17_0"]

if __name__ == "__main__":
    headers = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    tmpdir = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="curlw_")

    sys.stdin.reconfigure(line_buffering=True)
    sys.stdout.reconfigure(line_buffering=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "__EXIT__":
            break
        try:
            data = json.loads(line)
            url = data.get("url", line)
            req_timeout = data.get("timeout", 15)
            browser = random.choice(_IMPERSONATE_BROWSERS)
            resp = curl_requests.get(url, impersonate=browser, timeout=req_timeout, headers=headers)
            resp.raise_for_status()
            # Write response text to temp file
            id_ = data.get("id", str(random.randint(0, 2**31)))
            tmp = os.path.join(tmpdir, f"resp_{id_}.json")
            with open(tmp, "w") as f:
                json.dump({"ok": True, "text": resp.text}, f)
            out = {"ok": True, "file": tmp, "len": len(resp.text)}
        except Exception as e:
            out = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:300]}"}
        print(json.dumps(out), flush=True)
