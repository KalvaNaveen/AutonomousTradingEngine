"""
Headless Zerodha login using stored credentials + TOTP.
No browser. No human. Runs at 8:30 AM daily.

Flow:
  POST /api/login          → request_id
  POST /api/twofa          → request_token (in response data)
  kite.generate_session()  → access_token
  Write access_token       → .env
"""

import os
import time
import pyotp
import requests
from kiteconnect import KiteConnect
from dotenv import set_key
from config import (
    KITE_API_KEY, KITE_API_SECRET,
    ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET,
    ENV_FILE
)

KITE_LOGIN_URL  = "https://kite.zerodha.com/api/login"
KITE_TWOFA_URL  = "https://kite.zerodha.com/api/twofa"


class AutoLogin:

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":     ("Mozilla/5.0 (X11; Linux x86_64) "
                               "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
            "X-Kite-Version": "3",
            "Content-Type":   "application/x-www-form-urlencoded",
            "Referer":        "https://kite.zerodha.com/",
        })

    def _validate_env(self):
        missing = [k for k, v in {
            "KITE_API_KEY":        KITE_API_KEY,
            "KITE_API_SECRET":     KITE_API_SECRET,
            "ZERODHA_USER_ID":     ZERODHA_USER_ID,
            "ZERODHA_PASSWORD":    ZERODHA_PASSWORD,
            "ZERODHA_TOTP_SECRET": ZERODHA_TOTP_SECRET,
        }.items() if not v]
        if missing:
            raise RuntimeError(f"Missing .env keys: {missing}")

    def _get_fresh_totp(self) -> str:
        """Wait for a TOTP code with >5 seconds remaining validity."""
        totp = pyotp.TOTP(ZERODHA_TOTP_SECRET)
        remaining = 30 - (int(time.time()) % 30)
        if remaining < 5:
            time.sleep(remaining + 1)
        return totp.now()

    def login(self) -> str:
        """
        Full login. Returns access_token string.
        Raises RuntimeError on any step failure.
        """
        self._validate_env()

        # Step 1: Password login
        r1 = self.session.post(KITE_LOGIN_URL, data={
            "user_id":  ZERODHA_USER_ID,
            "password": ZERODHA_PASSWORD,
        }, timeout=20)

        if r1.status_code != 200:
            raise RuntimeError(f"Login failed HTTP {r1.status_code}: {r1.text[:200]}")

        d1 = r1.json()
        if d1.get("status") != "success":
            raise RuntimeError(f"Login rejected: {d1.get('message', d1)}")

        request_id = d1["data"]["request_id"]

        # Step 2: TOTP 2FA
        totp_code = self._get_fresh_totp()
        r2 = self.session.post(KITE_TWOFA_URL, data={
            "user_id":     ZERODHA_USER_ID,
            "request_id":  request_id,
            "twofa_value": totp_code,
            "twofa_type":  "totp",
        }, timeout=20)

        if r2.status_code != 200:
            raise RuntimeError(f"2FA failed HTTP {r2.status_code}: {r2.text[:200]}")

        d2 = r2.json()
        if d2.get("status") != "success":
            raise RuntimeError(f"2FA rejected: {d2.get('message', d2)}")

        # Step 3: Extract request_token
        request_token = d2.get("data", {}).get("request_token")
        if not request_token:
            # Fallback: check redirect URL in response history
            for resp in list(r2.history) + [r2]:
                url = getattr(resp, "url", "") or ""
                if "request_token=" in url:
                    request_token = url.split("request_token=")[1].split("&")[0]
                    break

        if not request_token:
            raise RuntimeError(
                "request_token not found in response. "
                "Verify Kite app redirect URL is configured correctly."
            )

        # Step 4: Exchange for access_token
        kite = KiteConnect(api_key=KITE_API_KEY)
        sess = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = sess["access_token"]

        # Step 5: Persist to .env and current process
        set_key(ENV_FILE, "KITE_ACCESS_TOKEN", access_token)
        os.environ["KITE_ACCESS_TOKEN"] = access_token

        return access_token

    def run(self, alert_fn=None) -> bool:
        """
        Called by scheduler at 8:30 AM.
        Returns True on success, False on failure.
        Always sends Telegram alert.
        """
        try:
            token = self.login()
            msg = (f"✅ *AUTO LOGIN SUCCESS*\n"
                   f"Token: `{token[:10]}...`\n"
                   f"Engine armed for 9:30 AM.")
            if alert_fn:
                alert_fn(msg)
            print(f"[AutoLogin] Success — {token[:10]}...")
            return True

        except Exception as e:
            msg = (f"🚨 *AUTO LOGIN FAILED*\n"
                   f"`{str(e)}`\n"
                   f"Engine will NOT trade today.\n"
                   f"Check .env credentials immediately.")
            if alert_fn:
                alert_fn(msg)
            print(f"[AutoLogin] FAILED: {e}")
            return False
