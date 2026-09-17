"""Account pool: registry, runtime state, health, selection, sticky sessions.

Design notes
------------
* Registry (id / label / enabled / state file) lives in ``accounts.json``.
* Runtime state (credits, counters, cooldown) lives in ``pool_state.json``.
  Keeping them apart means a corrupt state file can never take the registry
  with it, and the registry stays hand-editable.
* Credential material never leaves the per-account storage-state files. This
  module exposes paths and redacted metadata only.
* Selection is smooth weighted round-robin (SWRR) over effective weights, so
  long-run traffic follows the quota ratio without concentrating on one
  account and starving the others.
* A single enabled account short-circuits everything: no rotation, no sticky
  bookkeeping, legacy conversation keys. That keeps the single-account
  deployment byte-for-byte compatible with the previous release.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("loomy2api.pool")

# ---- cooldown kinds ---------------------------------------------------------
COOL_NONE = "none"
COOL_RATE = "rate"            # HTTP 429
COOL_UPSTREAM = "upstream"    # 5xx / network
COOL_EXHAUSTED = "exhausted"  # quota spent

# ---- auth states ------------------------------------------------------------
AUTH_OK = "ok"
AUTH_INVALID = "invalid"      # 401: no refresh token, needs manual re-login
AUTH_UNKNOWN = "unknown"

# ---- error classification ---------------------------------------------------
# Only these may be retried on another account before the first byte reaches
# the client. Everything else is terminal for the request.
FATAL_AUTH = "auth"           # 401 -> account invalid
RETRY_RATE = "rate"           # 429 -> soft cooldown
RETRY_UPSTREAM = "upstream"   # 5xx / network / busy -> breaker
CLASS_NONE = "none"


def classify_status(status_code: int, body: str = "") -> str:
    """Map an upstream failure to an account-pool error class.

    Deliberately distinguishes 'quota lookup failed' from 'account is broken':
    a refresh hiccup must not evict a healthy account from routing.
    """
    if status_code in (401, 403):
        return FATAL_AUTH
    if status_code == 429:
        return RETRY_RATE
    if status_code >= 500 or status_code in (408, 409, 425):
        return RETRY_UPSTREAM
    text = (body or "").lower()
    if "unauthenticated" in text or "请先登录" in text:
        return FATAL_AUTH
    if "rate" in text and "limit" in text:
        return RETRY_RATE
    return CLASS_NONE


@dataclass
class Account:
    """One Loomy account: static registry fields plus runtime health."""

    id: str
    label: str = ""
    enabled: bool = True
    state_file: str = ""
    fingerprint: str = ""
    created_at: float = 0.0

    # -- runtime, persisted to pool_state.json --
    credits: int | None = None
    daily: int | None = None
    credits_checked_at: float = 0.0
    credits_ok: bool = False
    masked_phone: str = ""
    nickname: str = ""
    auth_state: str = AUTH_UNKNOWN
    cooldown_until: float = 0.0
    cooldown_kind: str = COOL_NONE
    cooldown_strikes: int = 0
    consecutive_errors: int = 0
    success_count: int = 0
    err_count: int = 0
    quota_query_fail: int = 0
    last_success: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0

    # -- in-memory only, never persisted --
    inflight: int = 0
    swrr_current: float = 0.0

    def cooling(self, at: float | None = None) -> bool:
        return self.cooldown_until > (at if at is not None else time.time())

    def available(self, max_in_flight: int, at: float | None = None,
                  ignore_inflight: bool = False) -> bool:
        if not self.enabled:
            return False
        if self.auth_state == AUTH_INVALID:
            return False
        if self.cooldown_kind == COOL_EXHAUSTED:
            return False
        if self.cooling(at):
            return False
        if not ignore_inflight and self.inflight >= max_in_flight:
            return False
        return True


class AccountPool:
    """Registry + runtime state + selection policy for the Loomy account pool."""

    def __init__(self, settings):
        self.s = settings
        self.data_dir = Path(settings.data_dir)
        self.registry_path = Path(settings.accounts_file)
        self.accounts_dir = Path(settings.accounts_dir)
        self.pool_state_path = Path(settings.pool_state_file)
        self.legacy_state_path = Path(settings.session_state_path)
        self._lock = threading.RLock()
        self._accounts: dict[str, Account] = {}
        self._sticky: dict[str, tuple[str, float]] = {}
        self._rr_counter = 0
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        registry = self._read_json(self.registry_path) or {}
        entries = registry.get("accounts") or []
        if not entries:
            entries = self._bootstrap()
        state = self._read_json(self.pool_state_path) or {}
        saved = state.get("accounts") or {}
        for raw in entries:
            acc = Account(
                id=str(raw["id"]),
                label=str(raw.get("label") or raw["id"]),
                enabled=bool(raw.get("enabled", True)),
                state_file=str(raw.get("stateFile") or f"accounts/{raw['id']}.json"),
                fingerprint=str(raw.get("fingerprint") or ""),
                created_at=float(raw.get("createdAt") or 0.0),
            )
            self._apply_saved(acc, saved.get(acc.id) or {})
            self._accounts[acc.id] = acc
        self.default_id = str(registry.get("default") or next(iter(self._accounts), ""))
        if self.default_id not in self._accounts:
            self.default_id = next(iter(self._accounts), "")
        self._save_registry()
        self._save_state()
        log.info("account pool loaded: %d account(s), default=%s",
                 len(self._accounts), self.default_id or "-")

    def _bootstrap(self) -> list[dict]:
        """First run: adopt the legacy single-account storage state, if any.

        The legacy file is copied, never moved, so a rollback to the previous
        image keeps working exactly as before.
        """
        entries: list[dict] = []
        target = self.accounts_dir / "acc_main.json"
        if self.legacy_state_path.exists():
            try:
                data = self.legacy_state_path.read_bytes()
                if not target.exists():
                    target.write_bytes(data)
                entries.append({
                    "id": "acc_main",
                    "label": "主号",
                    "enabled": True,
                    "stateFile": "accounts/acc_main.json",
                    "createdAt": time.time(),
                })
                log.info("bootstrapped pool from legacy storage state")
            except Exception:
                log.exception("legacy storage-state adoption failed")
        else:
            entries.append({
                "id": "acc_main",
                "label": "主号",
                "enabled": True,
                "stateFile": "accounts/acc_main.json",
                "createdAt": time.time(),
            })
        return entries

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            log.exception("unreadable json: %s", path)
            return None

    def _write_json(self, path: Path, obj) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _save_registry(self) -> None:
        self._write_json(self.registry_path, {
            "version": 1,
            "default": self.default_id,
            "accounts": [
                {
                    "id": a.id,
                    "label": a.label,
                    "enabled": a.enabled,
                    "stateFile": a.state_file,
                    "fingerprint": a.fingerprint,
                    "createdAt": a.created_at,
                }
                for a in self._accounts.values()
            ],
        })

    _STATE_FIELDS = (
        "credits", "daily", "credits_checked_at", "credits_ok",
        "masked_phone", "nickname", "auth_state",
        "cooldown_until", "cooldown_kind", "cooldown_strikes", "consecutive_errors",
        "success_count", "err_count", "quota_query_fail", "last_success",
        "last_error", "last_error_at",
    )

    def _save_state(self) -> None:
        self._write_json(self.pool_state_path, {
            "version": 1,
            "saved_at": time.time(),
            "accounts": {
                a.id: {k: getattr(a, k) for k in self._STATE_FIELDS}
                for a in self._accounts.values()
            },
        })

    def _apply_saved(self, acc: Account, saved: dict) -> None:
        for k in self._STATE_FIELDS:
            if k in saved and saved[k] is not None:
                setattr(acc, k, saved[k])

    def save_state(self) -> None:
        with self._lock:
            self._save_state()

    # -- registry access ------------------------------------------------------

    def all(self) -> list[Account]:
        with self._lock:
            return list(self._accounts.values())

    def get(self, account_id: str) -> Account | None:
        with self._lock:
            return self._accounts.get(account_id)

    def default(self) -> Account | None:
        with self._lock:
            return self._accounts.get(self.default_id)

    def enabled_ids(self) -> list[str]:
        with self._lock:
            return [a.id for a in self._accounts.values() if a.enabled]

    def usable(self) -> list[Account]:
        """Accounts that may serve a request right now."""
        at = time.time()
        with self._lock:
            return [
                a for a in self._accounts.values()
                if a.available(self.s.pool_max_in_flight, at)
            ]

    @property
    def single_account(self) -> bool:
        """True when the deployment has exactly one registered account.

        Counted over registered, not enabled, accounts on purpose. An existing
        single-account deployment upgrading to this build must behave exactly as
        before. A deployment that added a second account has opted into pool
        semantics, and disabling one of its accounts must actually disable it
        rather than silently dropping back to the compatibility path that
        ignores enabled/auth/cooldown state.
        """
        with self._lock:
            return len(self._accounts) <= 1

    # -- credentials ----------------------------------------------------------

    def state_path(self, account: Account) -> Path:
        """Credential file for one account (storage-state JSON)."""
        return self.data_dir / account.state_file

    def has_credentials(self, account: Account) -> bool:
        p = self.state_path(account)
        try:
            return p.exists() and p.stat().st_size > 20
        except OSError:
            return False

    def cookie_header(self, account: Account) -> str:
        """Build the upstream Cookie header. Never logged, never returned."""
        raw = json.loads(self.state_path(account).read_text(encoding="utf-8"))
        cookies = {
            c["name"]: c["value"]
            for c in raw.get("cookies", [])
            if c.get("domain", "").endswith("loomy.xunfei.cn")
        }
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    def write_cookie(self, account: Account, cookie_string: str,
                     fingerprint: str = "") -> None:
        """Store a pasted cookie as a storage state.

        Accepts either a raw ``name=value; name=value`` header or a full
        storage-state JSON document, so the admin UI can take what the browser
        copy button yields.
        """
        text = (cookie_string or "").strip()
        if not text:
            raise ValueError("cookie is empty")
        if text.startswith("{"):
            doc = json.loads(text)
            if "cookies" not in doc:
                raise ValueError("storage state has no cookies")
        else:
            pairs = []
            for part in text.replace("\n", ";").split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                name, value = part.split("=", 1)
                pairs.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": "loomy.xunfei.cn",
                    "path": "/",
                })
            if not pairs:
                raise ValueError("no name=value cookie pairs found")
            doc = {
                "cookies": pairs,
                "origins": [{
                    "origin": "https://loomy.xunfei.cn",
                    "localStorage": [{
                        "name": "loomy.device.fingerprint",
                        "value": fingerprint or f"web_fp_{uuid.uuid4().hex[:8]}",
                    }],
                }],
            }
        path = self.state_path(account)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        # A pasted cookie must prove itself before the account is trusted.
        with self._lock:
            account.auth_state = AUTH_UNKNOWN
            account.cooldown_until = 0.0
            account.cooldown_kind = COOL_NONE
        if fingerprint:
            account.fingerprint = fingerprint

    def clear_credentials(self, account: Account) -> None:
        try:
            self.state_path(account).unlink(missing_ok=True)
        except OSError:
            log.exception("failed to remove state for %s", account.id)
        with self._lock:
            account.auth_state = AUTH_INVALID
            account.credits = None
            account.daily = None
            account.credits_ok = False

    # -- registry mutation ----------------------------------------------------

    def add(self, label: str = "", account_id: str = "") -> Account:
        with self._lock:
            aid = (account_id or "").strip() or f"acc_{uuid.uuid4().hex[:8]}"
            if aid in self._accounts:
                raise ValueError(f"account already exists: {aid}")
            acc = Account(
                id=aid,
                label=label.strip() or aid,
                enabled=True,
                state_file=f"accounts/{aid}.json",
                fingerprint=f"web_fp_{uuid.uuid4().hex[:8]}",
                created_at=time.time(),
            )
            self._accounts[aid] = acc
            if not self.default_id:
                self.default_id = aid
            self._save_registry()
            self._save_state()
            log.info("account added: %s", aid)
            return acc

    def remove(self, account_id: str) -> bool:
        with self._lock:
            acc = self._accounts.pop(account_id, None)
            if not acc:
                return False
            try:
                self.state_path(acc).unlink(missing_ok=True)
            except OSError:
                log.exception("failed to remove state for %s", account_id)
            self._sticky = {k: v for k, v in self._sticky.items() if v[0] != account_id}
            if self.default_id == account_id:
                self.default_id = next(iter(self._accounts), "")
            self._save_registry()
            self._save_state()
            log.info("account removed: %s", account_id)
            return True

    def set_enabled(self, account_id: str, enabled: bool) -> Account | None:
        with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return None
            acc.enabled = enabled
            if enabled:
                # Re-enabling re-qualifies the account: stale cooldowns and
                # breaker strikes must be re-earned, not inherited. An invalid
                # credential is a fact about the cookie, so it survives: only a
                # successful refresh clears it.
                acc.cooldown_until = 0.0
                acc.cooldown_kind = COOL_NONE
                acc.consecutive_errors = 0
                acc.cooldown_strikes = 0
            self._save_registry()
            self._save_state()
            log.info("account %s %s", account_id, "enabled" if enabled else "disabled")
            return acc

    # -- health recording -----------------------------------------------------

    def note_success(self, account: Account) -> None:
        """Record one completed turn. Call exactly once per finished request."""
        with self._lock:
            account.success_count += 1
            account.consecutive_errors = 0
            account.last_success = time.time()

    def note_credit_spend(self, account: Account, cost: int) -> None:
        """Record credits this turn consumed, without touching health counters.

        Kept separate from note_success on purpose: Loomy reports points from
        its own event inside the same turn, so folding this in incremented
        success_count twice per request and skewed the health weight that
        selection divides by. Refresh remains the authority on quota; this only
        keeps routing responsive between refreshes.
        """
        if not cost:
            return
        with self._lock:
            if account.credits is not None:
                account.credits = max(0, account.credits - int(cost))

    def note_failure(self, account: Account, kind: str, detail: str = "") -> None:
        now = time.time()
        with self._lock:
            account.err_count += 1
            account.consecutive_errors += 1
            account.last_error = _scrub(detail)[:300]
            account.last_error_at = now
            if kind == FATAL_AUTH:
                # Loomy issues no refresh token, so this cannot self-heal.
                account.auth_state = AUTH_INVALID
                account.cooldown_kind = COOL_NONE
                account.cooldown_until = 0.0
                log.warning("account %s auth invalid -> needs re-login", account.id)
            elif kind == RETRY_RATE:
                account.cooldown_strikes += 1
                delay = min(
                    self.s.pool_soft_rate_cooldown * (2 ** (account.cooldown_strikes - 1)),
                    self.s.pool_soft_rate_cooldown_max,
                )
                account.cooldown_kind = COOL_RATE
                account.cooldown_until = now + delay
                log.warning("account %s rate limited -> cooldown %ds", account.id, delay)
            elif kind == RETRY_UPSTREAM:
                if account.consecutive_errors >= self.s.pool_breaker_threshold:
                    account.cooldown_strikes += 1
                    delay = min(
                        self.s.pool_breaker_cooldown * (2 ** (account.cooldown_strikes - 1)),
                        self.s.pool_breaker_cooldown_max,
                    )
                    account.cooldown_kind = COOL_UPSTREAM
                    account.cooldown_until = now + delay
                    log.warning("account %s breaker open -> cooldown %ds", account.id, delay)

    def note_credits(self, account: Account, credits: int | None, daily: int | None,
                     ok: bool, error: str = "") -> None:
        """Record a quota lookup.

        A failed lookup is NOT an account failure: the last known value is
        kept so routing stays sane, and only the failure counter moves.
        """
        with self._lock:
            if ok:
                account.credits = credits
                account.daily = daily
                account.credits_checked_at = time.time()
                account.credits_ok = True
                account.quota_query_fail = 0
                account.auth_state = AUTH_OK
                if account.cooldown_kind == COOL_EXHAUSTED:
                    account.cooldown_kind = COOL_NONE
                    account.cooldown_until = 0.0
            else:
                account.quota_query_fail += 1
                account.credits_ok = False
                account.last_error = _scrub(error)[:300]
                account.last_error_at = time.time()

    def mark_exhausted(self, account: Account) -> None:
        with self._lock:
            account.cooldown_kind = COOL_EXHAUSTED
            account.cooldown_until = 0.0

    def mark_auth_invalid(self, account: Account, detail: str = "") -> None:
        with self._lock:
            account.auth_state = AUTH_INVALID
            account.last_error = _scrub(detail)[:300]
            account.last_error_at = time.time()

    # -- selection ------------------------------------------------------------

    def _weight(self, acc: Account, usable: list[Account], now: float) -> float:
        """Effective weight: quota share x health x idleness / concurrency."""
        credits = acc.credits
        known = [a.credits for a in usable if a.credits is not None]
        if credits is None or not known:
            quota = 1.0
        else:
            top = max(known) or 1
            share = max(0.0, min(1.0, credits / top))
            floor = self.s.pool_quota_floor
            quota = floor + (1.0 - floor) * share

        health = (1.0 + acc.success_count) / (
            1.0 + acc.success_count + 3.0 * acc.err_count
        )

        idle_hours = (now - acc.last_success) / 3600.0 if acc.last_success else 0.0
        idle = min(1.0 + idle_hours * self.s.pool_idle_weight_per_hour,
                   self.s.pool_idle_weight_max)

        return quota * health * idle / (1.0 + acc.inflight)

    def _swrr_pick(self, usable: list[Account], now: float) -> Account:
        """Smooth weighted round-robin (nginx algorithm)."""
        total = 0.0
        best: Account | None = None
        for acc in usable:
            w = self._weight(acc, usable, now)
            acc.swrr_current += w
            total += w
            if best is None or acc.swrr_current > best.swrr_current:
                best = acc
        assert best is not None
        best.swrr_current -= total
        return best

    def acquire(self, session_key: str = "", exclude=()) -> tuple[Account, str] | tuple[None, str]:
        """Pick an account and reserve one in-flight slot.

        Sticky sessions win while their account stays usable, because Loomy
        conversation ids are per-account: switching mid-conversation loses the
        upstream context. Returns (account, reason) so the caller can log why
        this account was chosen.

        ``exclude`` lists account ids already tried for this request. Failover
        must not hand the request back to the account that just failed, or the
        same failure repeats and the client waits through every attempt for
        nothing.
        """
        skip = set(exclude)
        now = time.time()
        with self._lock:
            usable = [
                a for a in self._accounts.values()
                if a.id not in skip and a.available(self.s.pool_max_in_flight, now)
            ]
            if not usable:
                return None, "none_available"

            reason = "weighted"
            chosen: Account | None = None
            keep_pin = False
            if session_key and self.s.pool_sticky_enabled:
                pinned = self._sticky.get(session_key)
                if pinned:
                    acc = self._accounts.get(pinned[0])
                    if acc is not None and acc in usable:
                        chosen = acc
                        reason = "sticky"
                    elif acc is not None and acc.available(
                        self.s.pool_max_in_flight, now, ignore_inflight=True
                    ):
                        # The pinned account is healthy but busy. Routing this
                        # request elsewhere is a transient capacity decision, so
                        # the pin is kept: re-pinning here would permanently
                        # migrate the session's upstream conversation away from
                        # the account that holds its history.
                        keep_pin = True
                        reason = "sticky_busy"
            if chosen is None:
                chosen = self._swrr_pick(usable, now)
                if reason == "weighted" and len(usable) == 1:
                    reason = "single"
            if session_key and not keep_pin:
                self._sticky[session_key] = (chosen.id, now)
            chosen.inflight += 1
            return chosen, reason

    def release(self, account: Account | None) -> None:
        if account is None:
            return
        with self._lock:
            account.inflight = max(0, account.inflight - 1)

    def drop_sticky(self, session_key: str) -> None:
        with self._lock:
            self._sticky.pop(session_key, None)

    def gc_sticky(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        ttl = self.s.pool_sticky_ttl
        with self._lock:
            stale = [k for k, v in self._sticky.items() if now - v[1] > ttl]
            for k in stale:
                self._sticky.pop(k, None)
            return len(stale)

    # -- reporting ------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        """Redacted view for /credit and the admin UI. No credentials inside."""
        now = time.time()
        with self._lock:
            rows = []
            for a in self._accounts.values():
                rows.append({
                    "id": a.id,
                    "label": a.label,
                    "enabled": a.enabled,
                    "state": self.describe_state(a, now),
                    "credits": a.credits,
                    "daily": a.daily,
                    "credits_ok": a.credits_ok,
                    "credits_checked_at": a.credits_checked_at or None,
                    "credits_age": round(now - a.credits_checked_at, 1) if a.credits_checked_at else None,
                    "auth_state": a.auth_state,
                    "has_credentials": self.has_credentials(a),
                    "cooldown_kind": a.cooldown_kind,
                    "cooldown_until": a.cooldown_until or None,
                    "cooldown_remaining": max(0, round(a.cooldown_until - now)) if a.cooling(now) else 0,
                    "inflight": a.inflight,
                    "success_count": a.success_count,
                    "err_count": a.err_count,
                    "quota_query_fail": a.quota_query_fail,
                    "last_success": a.last_success or None,
                    "last_error": a.last_error or None,
                    "last_error_at": a.last_error_at or None,
                    "sticky_sessions": sum(1 for v in self._sticky.values() if v[0] == a.id),
                })
            return rows

    @staticmethod
    def describe_state(a: Account, now: float) -> str:
        if not a.enabled:
            return "disabled"
        if a.auth_state == AUTH_INVALID:
            return "needs_relogin"
        if a.cooldown_kind == COOL_EXHAUSTED:
            return "exhausted"
        if a.cooling(now):
            return "rate_limited" if a.cooldown_kind == COOL_RATE else "cooling"
        if not a.credits_ok and a.quota_query_fail > 0:
            return "degraded"
        return "ok"


def _scrub(text: str) -> str:
    """Strip anything credential-shaped before it reaches a log or the UI."""
    if not text:
        return ""
    out = str(text)
    for marker in ("loomy_web_session=", "Cookie:", "Authorization:", "Bearer "):
        idx = out.find(marker)
        if idx >= 0:
            out = out[:idx] + marker + "<redacted>"
    return out


# Re-exported so callers do not need to know the module layout.
__all__ = [
    "Account", "AccountPool", "classify_status",
    "AUTH_OK", "AUTH_INVALID", "AUTH_UNKNOWN",
    "COOL_NONE", "COOL_RATE", "COOL_UPSTREAM", "COOL_EXHAUSTED",
    "FATAL_AUTH", "RETRY_RATE", "RETRY_UPSTREAM", "CLASS_NONE",
]
