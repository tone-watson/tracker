"""Verify Tracker's identity as well as HTTP liveness on its loopback listener."""

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import build_opener

from .auth import NoRedirect


def check(port=8792, opener=None):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Tracker port must be between 1 and 65535")
    opener = opener or build_opener(NoRedirect)
    with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
        payload = response.read(4097)
        if len(payload) > 4096:
            raise ValueError("Unexpected health response")
        result = json.loads(payload)
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("data"), dict)
        or result["data"].get("contract") != "farm.tracker/1"
        or result["data"].get("status") != "ok"
    ):
        raise ValueError("Listener did not identify itself as a healthy Tracker service")
    return result


def main():
    try:
        result = check(int(os.environ.get("TRACKER_PORT", "8792")))
    except (ValueError, OSError, HTTPError, URLError):
        print(
            "Tracker health check failed: no healthy farm.tracker/1 service at the configured loopback port",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
