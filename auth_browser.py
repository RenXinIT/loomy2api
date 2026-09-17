import logging, threading, time
from pathlib import Path
from playwright.sync_api import sync_playwright

log = logging.getLogger("loomy2api.auth")


class LoginBrowser:
    """Interactive Loomy login for one account at a time.

    Loomy authenticates with a single `loomy_web_session` cookie and issues no
    refresh token, so a browser session is the only way to (re)qualify an
    account. Each account keeps its own storage-state file: logging in account
    B must not disturb account A's cookie.
    """

    def __init__(self, settings, pool=None):
        self.s = settings
        self.pool = pool
        self.state = Path(settings.session_state_path)  # legacy/default path
        self.thread = None
        self.error = None
        self.account_id = None

    # -- target resolution ---------------------------------------------------

    def _target(self, account_id: str | None):
        """Return (account, state_path). Falls back to the legacy path."""
        if self.pool is not None:
            if account_id:
                acc = self.pool.get(account_id)
            else:
                acc = self.pool.default() or (self.pool.all() or [None])[0]
            if acc is not None:
                return acc, self.pool.state_path(acc)
        return None, self.state

    def session_exists(self, account_id: str | None = None) -> bool:
        _, path = self._target(account_id)
        try:
            return path.exists() and path.stat().st_size > 20
        except OSError:
            return False

    def status(self, account_id: str | None = None):
        acc, path = self._target(account_id)
        return {
            "logged_in": self.session_exists(account_id),
            "account": acc.id if acc else None,
            "state_file": str(path),
            "browser_running": bool(self.thread and self.thread.is_alive()),
            "current_account": self.account_id,
            "error": self.error,
        }

    # -- browser lifecycle ---------------------------------------------------

    def start(self, account_id: str | None = None):
        if self.thread and self.thread.is_alive():
            return
        acc, _ = self._target(account_id)
        self.account_id = acc.id if acc else account_id
        self.error = None
        self.thread = threading.Thread(target=self._run, args=(account_id,), daemon=True)
        self.thread.start()

    def clear(self, account_id: str | None = None):
        acc, path = self._target(account_id)
        path.unlink(missing_ok=True)
        if acc is not None and self.pool is not None:
            self.pool.mark_auth_invalid(acc, "credentials cleared")

    def _run(self, account_id: str | None):
        acc, path = self._target(account_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                ctx = browser.new_context(
                    storage_state=str(path) if self.session_exists(account_id) else None
                )
                page = ctx.new_page()
                page.goto(self.s.loomy_web_url, wait_until="domcontentloaded", timeout=60000)
                consecutive_failures = 0
                while True:
                    try:
                        ctx.storage_state(path=str(path))
                        consecutive_failures = 0
                    except Exception:
                        # A page mid-navigation can make this throw transiently;
                        # only give up if it keeps failing.
                        consecutive_failures += 1
                        log.warning("storage_state save failed (%d consecutive)",
                                    consecutive_failures)
                    time.sleep(3)
        except Exception as e:
            self.error = str(e)
            log.exception("login browser stopped")
