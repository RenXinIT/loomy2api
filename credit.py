"""Quota lookup, cache, and the background refresher.

The Loomy web backend exposes the same endpoints its own dashboard uses:

    GET /web/api/auth/me             -> {userId, phone, maskedPhone, loggedInAt}
    GET /web/api/auth/profile        -> {nickname, headpic, sign}
    GET /web/api/auth/points-summary -> {permanent, daily}

Authentication is a single `loomy_web_session` cookie; there is no bearer
token and no CSRF header. A dead cookie answers 401 with
`{"code":"UNAUTHENTICATED"}`, which is what the health probe keys on.

Routing reads only the cache in here. No request path ever calls upstream for
quota, so a slow or failing quota endpoint can never slow down or fail chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from accounts import Account, AccountPool, classify_status, FATAL_AUTH

log = logging.getLogger("loomy2api.credit")

API_ME = "/web/api/auth/me"
API_PROFILE = "/web/api/auth/profile"
API_POINTS = "/web/api/auth/points-summary"

DEFAULT_BASE = "https://loomy.xunfei.cn"


class CreditService:
    """Fetches and caches per-account quota and identity."""

    def __init__(self, settings, pool: AccountPool):
        self.s = settings
        self.pool = pool
        self.base = (settings.loomy_base_url or DEFAULT_BASE).rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._refresh_lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=20, write=10, pool=10),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
            log.info("credit refresher started (interval=%ss)", self.s.credit_refresh_interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self.aclose()

    async def _loop(self) -> None:
        # First pass runs promptly so /credit is useful right after boot, but a
        # little jitter avoids hammering upstream in lockstep with the gateway.
        await asyncio.sleep(3)
        while not self._stop.is_set():
            try:
                await self.refresh_all(reason="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduled credit refresh failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.s.credit_refresh_interval
                )
            except asyncio.TimeoutError:
                pass

    # -- fetching ------------------------------------------------------------

    def _headers(self, account: Account) -> dict:
        return {
            "Accept": "application/json",
            "Cookie": self.pool.cookie_header(account),
            "Referer": f"{self.base}/web",
            "Origin": self.base,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/140 Safari/537.36",
        }

    async def _get_json(self, account: Account, path: str):
        r = await self.client().get(self.base + path, headers=self._headers(account))
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        body = r.text[:300]
        err = _UpstreamAuth(r.status_code, body)
        raise err

    async def fetch_one(self, account: Account) -> dict:
        """Refresh one account. Never raises: failures become recorded state."""
        if not self.pool.has_credentials(account):
            self.pool.mark_auth_invalid(account, "no stored credentials")
            return {"id": account.id, "ok": False, "reason": "no_credentials"}

        try:
            points = await self._get_json(account, API_POINTS)
        except _UpstreamAuth as exc:
            klass = classify_status(exc.status, exc.body)
            if klass == FATAL_AUTH:
                # Distinguish a dead account from a dead quota endpoint: only a
                # real 401/403 verdict is allowed to evict the account.
                self.pool.mark_auth_invalid(account, f"quota lookup: {exc.status}")
                return {"id": account.id, "ok": False, "reason": "auth_invalid"}
            self.pool.note_credits(account, account.credits, account.daily, False,
                                   f"quota lookup http {exc.status}")
            return {"id": account.id, "ok": False, "reason": f"http_{exc.status}"}
        except Exception as exc:
            # Network hiccup / timeout: keep last known quota, count the miss.
            self.pool.note_credits(account, account.credits, account.daily, False,
                                   f"quota lookup failed: {type(exc).__name__}")
            return {"id": account.id, "ok": False, "reason": "network"}

        data = (points or {}).get("data") or {}
        permanent = _as_int(data.get("permanent"))
        daily = _as_int(data.get("daily"))

        # Identity is best-effort decoration: a failure here must not invalidate
        # a successful quota reading.
        meta = {}
        for path, key in ((API_ME, "me"), (API_PROFILE, "profile")):
            try:
                doc = await self._get_json(account, path)
                meta[key] = (doc or {}).get("data") or {}
            except Exception:
                meta[key] = {}

        self.pool.note_credits(account, permanent, daily, True)
        if meta.get("me", {}).get("maskedPhone"):
            account.masked_phone = meta["me"]["maskedPhone"]
        if meta.get("profile", {}).get("nickname"):
            account.nickname = meta["profile"]["nickname"]

        if permanent is not None and permanent <= 0:
            self.pool.mark_exhausted(account)

        return {
            "id": account.id,
            "ok": True,
            "credits": permanent,
            "daily": daily,
        }

    async def refresh_all(self, reason: str = "manual") -> dict:
        """Refresh every account sequentially with a small gap.

        Sequential by design: these endpoints are cheap but rate-sensitive, and
        a burst of parallel quota calls is the fastest way to get throttled.
        """
        async with self._refresh_lock:
            started = time.time()
            results = []
            for account in self.pool.all():
                if not account.enabled:
                    continue
                results.append(await self.fetch_one(account))
                await asyncio.sleep(self.s.credit_refresh_gap)
            self.pool.save_state()
            ok = sum(1 for r in results if r.get("ok"))
            log.info("credit refresh (%s): %d/%d ok in %.1fs",
                     reason, ok, len(results), time.time() - started)
            return {"ok": ok, "total": len(results), "results": results}

    async def refresh_account(self, account_id: str) -> dict:
        account = self.pool.get(account_id)
        if account is None:
            return {"error": "not_found"}
        async with self._refresh_lock:
            result = await self.fetch_one(account)
            self.pool.save_state()
            return result

    # -- reporting -----------------------------------------------------------

    def credit_payload(self) -> dict:
        """Cache-only snapshot shaped like the workbuddy /credit response."""
        rows = []
        for row in self.pool.snapshot():
            acc = self.pool.get(row["id"])
            rows.append({
                "id": row["id"],
                "label": row["label"],
                # Masked identity only, and null rather than "" so a client can
                # tell 'not fetched yet' apart from 'fetched, empty'.
                "phone": getattr(acc, "masked_phone", None) or None,
                "nickname": getattr(acc, "nickname", None) or None,
                "remain": row["credits"],
                "daily": row["daily"],
                "percentage": None,
                "healthy": row["state"] == "ok",
                "state": row["state"],
                "auth_state": row["auth_state"],
                "rate_limited": row["cooldown_kind"] == "rate",
                "enabled": row["enabled"],
                "inflight": row["inflight"],
                "requests": row["success_count"] + row["err_count"],
                "success": row["success_count"],
                "errors": row["err_count"],
                "quota_query_fail": row["quota_query_fail"],
                "checked_at": row["credits_checked_at"],
                "checked_age": row["credits_age"],
                "cooldown_remaining": row["cooldown_remaining"],
                "last_error": row["last_error"],
            })
        known = [r["remain"] for r in rows if r["remain"] is not None]
        return {
            "service": "loomy2api",
            "accounts": rows,
            "total": {
                "accounts": len(rows),
                "enabled": sum(1 for r in rows if r["enabled"]),
                "healthy": sum(1 for r in rows if r["healthy"]),
                "remain": sum(known) if known else None,
            },
            "ts": int(time.time()),
            "queried_at": int(time.time()),
        }


class _UpstreamAuth(Exception):
    """Carries an HTTP failure from a quota endpoint."""

    def __init__(self, status: int, body: str):
        super().__init__(f"http {status}")
        self.status = status
        self.body = body


def _as_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
