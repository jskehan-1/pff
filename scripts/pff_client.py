"""
Thin authenticated client for the PFF Developer API (https://developer.pff.com).

Reads PFF_API_KEY from a .env file in the project root (one directory up
from this scripts/ folder).

Usage:
    from pff_client import PFFClient
    client = PFFClient()
    data = client.get("/v1/player/passing/summary",
                       params={"league": "nfl", "season": 2026, "week": 1})
"""
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# Load .env from the project root regardless of the caller's cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

BASE_URL = "https://api.pff.com"


class PFFClient:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.getenv("PFF_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "No PFF_API_KEY found. Set it in the .env file at the "
                f"project root ({PROJECT_ROOT / '.env'})."
            )
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})

    def get(self, path, params=None, max_retries=5):
        """
        GET with automatic retry on 429 rate-limit responses. PFF's error
        body includes details.retry_after_s -- confirmed live: the account's
        read budget is 100 calls/minute, shared across every endpoint. A
        weekly pull (4 report calls + 1 per franchise for team defense) can
        exceed that within a single week, let alone a multi-week range, so
        this backs off and retries rather than failing the whole run.
        """
        url = f"{BASE_URL}{path}"
        for attempt in range(max_retries + 1):
            resp = self.session.get(url, params=params or {})
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 and attempt < max_retries:
                wait_s = 20
                try:
                    wait_s = resp.json()["error"]["details"]["retry_after_s"]
                except (ValueError, KeyError, TypeError):
                    pass
                print(f"  [rate limit] hit 429 on {path}, waiting {wait_s}s "
                      f"(attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_s + 1)
                continue
            raise RuntimeError(
                f"PFF API error {resp.status_code} calling {path} "
                f"(params={params}): {resp.text[:500]}"
            )
        raise RuntimeError(f"Still rate-limited after {max_retries} retries calling {path}")

    def whoami(self):
        return self.get("/v1/auth/whoami")


if __name__ == "__main__":
    client = PFFClient()
    print(client.whoami())
