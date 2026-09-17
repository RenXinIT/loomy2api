import asyncio
import json
import logging

import httpx

# Loomy signals overload/racing through a 200 + JSON body rather than an HTTP
# error code. These markers (and 409/429/5xx) are worth retrying.
_TRANSIENT_MARKERS = ("conversation_busy", "nonterminal run", "900000", "Athena")


class LoomyError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def _is_transient(status_code: int, body_text: str) -> bool:
    if status_code in (409, 425, 429) or status_code >= 500:
        return True
    return any(marker in body_text for marker in _TRANSIENT_MARKERS)


class LoomyClient:
    MAX_ATTEMPTS = 3

    def __init__(self, settings):
        self.s = settings
        self.c = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20, read=None, write=20, pool=20),
            follow_redirects=True,
        )

    def build_payload(self, conversation_id, message_id, model, content):
        return {
            "conversationId": conversation_id or "",
            "messageId": message_id,
            "model": model,
            "content": content,
        }

    def headers(self, cookie: str):
        """Build upstream headers for one account's cookie.

        The cookie is passed in rather than read from a fixed path: with a pool
        the caller decides which account this request belongs to. It is never
        logged.
        """
        return {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": "https://loomy.xunfei.cn",
            "Referer": "https://loomy.xunfei.cn/web",
            "Cookie": cookie,
        }

    async def open_stream(self, payload, cookie: str):
        """Open the upstream stream, retrying transient overload.

        A successful Loomy response is always `text/event-stream`. Failures
        arrive as HTTP 200 with a JSON body (for example Athena 900000), which
        the naive proxy turned into a silent empty stream -- that is what made
        OpenClaw show a broken turn with no reply. Such bodies are now raised as
        errors instead.

        Retries here stay on the same account: this is transport-level recovery.
        Moving to another account is decided by the caller, before any byte
        reaches the client.
        """
        last_error = LoomyError("Loomy upstream unavailable", 502)
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            request = self.c.build_request(
                "POST", self.s.loomy_api, headers=self.headers(cookie), json=payload
            )
            response = await self.c.send(request, stream=True)
            if response.status_code == 200:
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    return response
                body = (await response.aread()).decode(errors="replace")
                await response.aclose()
                last_error = LoomyError(f"Loomy upstream error: {body[:500]}", 502)
                logging.warning(
                    "Loomy returned non-SSE 200 (attempt %d/%d): %s",
                    attempt, self.MAX_ATTEMPTS, _tail(body, 200),
                )
                if not _is_transient(200, body):
                    raise last_error
            else:
                body = (await response.aread()).decode(errors="replace")
                await response.aclose()
                last_error = LoomyError(
                    f"Loomy HTTP {response.status_code}: {body[:500]}",
                    response.status_code,
                )
                logging.warning(
                    "Loomy HTTP %d (attempt %d/%d): %s",
                    response.status_code, attempt, self.MAX_ATTEMPTS, _tail(body, 200),
                )
                if not _is_transient(response.status_code, body):
                    raise last_error
            if attempt < self.MAX_ATTEMPTS:
                await asyncio.sleep(2.0 * attempt)
        raise last_error

    async def iter_sse(self, response):
        buffer = []
        line_count = 0
        event_count = 0
        async for line in response.aiter_lines():
            line_count += 1
            logging.debug("RAW_UPSTREAM_LINE n=%d content=%r", line_count, line[:1000])
            if line == "":
                if buffer:
                    data = "\n".join(buffer)
                    buffer = []
                    event_count += 1
                    yield self.event(data)
                continue
            if line.startswith("data:"):
                buffer.append(line[5:].lstrip())
                continue
            # A bare JSON object instead of an SSE frame means Loomy reported a
            # failure (e.g. Athena 900000) on an otherwise healthy connection.
            stripped = line.strip()
            if stripped.startswith("{") and '"code"' in stripped:
                raise LoomyError(f"Loomy upstream error: {stripped[:300]}", 502)
        if buffer:
            data = "\n".join(buffer)
            event_count += 1
            yield self.event(data)
        logging.debug("RAW_SSE_SUMMARY lines=%d events=%d", line_count, event_count)

    def event(self, data):
        data = data.strip()
        if data == "[DONE]":
            return {"json": None, "raw": "data: [DONE]\n\n", "done": True}
        try:
            obj = json.loads(data)
            return {
                "json": obj,
                "raw": "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n\n",
                "done": False,
            }
        except Exception:
            return {"json": None, "raw": f"data: {data}\n\n", "done": False}

    @staticmethod
    def extract_conversation_id(obj):
        if not isinstance(obj, dict):
            return None
        vals = [obj.get("conversationId"), obj.get("conversation_id")]
        if isinstance(obj.get("loomy"), dict):
            vals += [obj["loomy"].get("conversation_id"), obj["loomy"].get("conversationId")]
        return next((str(v) for v in vals if v), None)

    @staticmethod
    def extract_turn_points(obj) -> int | None:
        """Read the credits this turn consumed, if upstream reports them.

        Field names mirror what the Loomy frontend accepts (points_consumed,
        cost_points, consumed_points, points). Used only to keep routing
        responsive between quota refreshes; the refresher stays the authority.
        """
        if not isinstance(obj, dict):
            return None
        usage = obj.get("usage")
        candidates = []
        if isinstance(usage, dict):
            candidates.append(usage)
        candidates.append(obj)
        for container in candidates:
            for key in ("points_consumed", "cost_points", "consumed_points",
                        "total_points", "points"):
                value = container.get(key)
                if value is None or value == "":
                    continue
                try:
                    number = int(float(value))
                except (TypeError, ValueError):
                    continue
                if number >= 0:
                    return number
        return None


def _tail(text: str, limit: int) -> str:
    """Truncate upstream error text for logs without leaking cookie material."""
    if not text:
        return ""
    out = text
    for marker in ("loomy_web_session=", "Cookie:"):
        idx = out.find(marker)
        if idx >= 0:
            out = out[:idx] + marker + "<redacted>"
    return out[:limit]
