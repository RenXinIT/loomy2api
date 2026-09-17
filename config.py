import os
from dataclasses import dataclass, field

# Canonical Loomy Web model IDs. Keep aliases separate from the public catalog so
# /v1/models never returns duplicate rows or alias-only IDs.
LOOMY_MODELS = [
    "deepseek-v4-flash-0731",
    "MiniMax-M3",
    "Kimi-k2.6",
    "qwen-3.8-max",
    "qwen3.8-flash",
    "GLM-5.3-Flash",
    "spark-x",
    "doubao-seed-2.0-mini",
    "mimo-v2.5",
    "qwen3.5-flash",
]

MODEL_ALIASES = {
    "deepseek-v4": "deepseek-v4-flash-0731",
    "deepseek-v4-flash": "deepseek-v4-flash-0731",
    "deepseek-v4-flash-0731": "deepseek-v4-flash-0731",
    "minimax-m3": "MiniMax-M3",
    "minimax": "MiniMax-M3",
    "MiniMax-M3": "MiniMax-M3",
    "kimi": "Kimi-k2.6",
    "kimi-k2.6": "Kimi-k2.6",
    "Kimi-k2.6": "Kimi-k2.6",
    "qwen": "qwen-3.8-max",
    "qwen-3.8-max": "qwen-3.8-max",
    "qwen3.8-flash": "qwen3.8-flash",
    "glm-5.3-flash": "GLM-5.3-Flash",
    "GLM-5.3-Flash": "GLM-5.3-Flash",
    "spark": "spark-x",
    "spark-x": "spark-x",
    "doubao": "doubao-seed-2.0-mini",
    "doubao-seed-2.0-mini": "doubao-seed-2.0-mini",
    "mimo": "mimo-v2.5",
    "mimo-v2.5": "mimo-v2.5",
    "qwen3.5-flash": "qwen3.5-flash",
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class Settings:
    api_key: str = os.getenv("LOOMY_API_KEY", "").strip()
    loomy_api: str = os.getenv("LOOMY_API", "https://loomy.xunfei.cn/web/api/chat/completions")
    loomy_web_url: str = os.getenv("LOOMY_WEB_URL", "https://loomy.xunfei.cn/web")
    loomy_base_url: str = os.getenv("LOOMY_BASE_URL", "https://loomy.xunfei.cn")
    session_state_path: str = os.getenv("SESSION_STATE_PATH", "/app/data/loomy-storage-state.json")
    database_path: str = os.getenv("DATABASE_PATH", "/app/data/loomy2api.db")
    public_host: str = os.getenv("PUBLIC_HOST", "localhost")
    port: int = int(os.getenv("PORT", "7865"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    model_aliases: dict[str, str] = field(default_factory=lambda: dict(MODEL_ALIASES))
    models: list[str] = field(default_factory=lambda: list(LOOMY_MODELS))

    # ---- account pool ------------------------------------------------------
    # data_dir anchors the registry and per-account credential files. It is
    # derived from the database path so existing deployments keep one volume.
    data_dir: str = os.getenv(
        "DATA_DIR",
        os.path.dirname(os.getenv("DATABASE_PATH", "/app/data/loomy2api.db")) or "/app/data",
    )
    accounts_file: str = os.getenv("ACCOUNTS_FILE", "")
    accounts_dir: str = os.getenv("ACCOUNTS_DIR", "")
    pool_state_file: str = os.getenv("POOL_STATE_FILE", "")

    # Quota refresh. The interval is the routing freshness knob: routing reads
    # cache only, so too small a value wastes upstream calls without improving
    # accuracy much.
    credit_refresh_interval: int = _env_int("CREDIT_REFRESH_INTERVAL", 45)
    credit_refresh_gap: float = _env_float("CREDIT_REFRESH_GAP", 0.8)

    pool_max_in_flight: int = _env_int("POOL_MAX_IN_FLIGHT", 3)
    pool_breaker_threshold: int = _env_int("POOL_BREAKER_THRESHOLD", 3)
    pool_breaker_cooldown: int = _env_int("POOL_BREAKER_COOLDOWN", 1800)
    pool_breaker_cooldown_max: int = _env_int("POOL_BREAKER_COOLDOWN_MAX", 21600)
    pool_soft_rate_cooldown: int = _env_int("POOL_SOFT_RATE_COOLDOWN", 600)
    pool_soft_rate_cooldown_max: int = _env_int("POOL_SOFT_RATE_COOLDOWN_MAX", 7200)
    pool_idle_weight_per_hour: float = _env_float("POOL_IDLE_WEIGHT_PER_HOUR", 0.5)
    pool_idle_weight_max: float = _env_float("POOL_IDLE_WEIGHT_MAX", 5.0)
    # Keeps a low-quota account in rotation instead of starving it entirely.
    pool_quota_floor: float = _env_float("POOL_QUOTA_FLOOR", 0.15)
    pool_sticky_enabled: bool = _env_bool("POOL_STICKY_ENABLED", True)
    pool_sticky_ttl: int = _env_int("POOL_STICKY_TTL", 1800)
    pool_sticky_gc_interval: int = _env_int("POOL_STICKY_GC_INTERVAL", 300)
    pool_failover_enabled: bool = _env_bool("POOL_FAILOVER_ENABLED", True)
    # Bounded so a fully broken pool cannot hold a client request open forever.
    pool_failover_attempts: int = _env_int("POOL_FAILOVER_ATTEMPTS", 3)
    # Total wall-clock budget for one request's cross-account failover. Only
    # applied in pool mode: with a single account the retry timing stays exactly
    # as before, so an upgrade changes nothing observable.
    pool_failover_deadline: float = _env_float("POOL_FAILOVER_DEADLINE", 20.0)
    pool_state_save_interval: int = _env_int("POOL_STATE_SAVE_INTERVAL", 30)

    # ---- admin -------------------------------------------------------------
    # Empty means admin writes are refused rather than left open. Reads stay
    # available so /credit keeps working for monitoring.
    admin_token: str = os.getenv("ADMIN_TOKEN", "").strip()

    def __post_init__(self) -> None:
        base = self.data_dir.rstrip("/") or "/app/data"
        self.data_dir = base
        if not self.accounts_file:
            self.accounts_file = f"{base}/accounts.json"
        if not self.accounts_dir:
            self.accounts_dir = f"{base}/accounts"
        if not self.pool_state_file:
            self.pool_state_file = f"{base}/pool_state.json"

    def resolve_model(self, requested: str) -> str | None:
        return self.model_aliases.get(requested)


settings = Settings()
