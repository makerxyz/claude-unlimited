"""Classify one local status snapshot without exposing its contents in logs."""

import json
import math
import sys


def classify_status(raw: str, minimum_idle: float) -> str:
    """Unknown is never idle: an unresponsive gateway can still be serving."""
    try:
        status = json.loads(raw)
        if not isinstance(status, dict) or status.get("status") != "ok":
            return "unknown"
        serving = status["serving_now"]
        idle = status["idle_seconds"]
        if not isinstance(serving, list):
            return "unknown"
        if serving:
            return "busy"
        if idle is None:  # a healthy gateway which has never served a request
            return "idle"
        if isinstance(idle, bool) or not isinstance(idle, (int, float)):
            return "unknown"
        if not math.isfinite(idle) or idle < 0:
            return "unknown"
        return "idle" if idle >= minimum_idle else "busy"
    except (KeyError, ValueError, TypeError):
        return "unknown"


if __name__ == "__main__":
    minimum_idle = float(sys.argv[1])
    if not math.isfinite(minimum_idle) or minimum_idle < 0:
        raise SystemExit("minimum idle seconds must be finite and non-negative")
    print(classify_status(sys.stdin.read(), minimum_idle))
