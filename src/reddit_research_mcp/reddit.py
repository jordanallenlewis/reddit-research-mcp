"""HTTP, authentication and rate limiting for the Reddit API.

redditwarp supplies OAuth token acquisition and renewal and the httpx
transport. Endpoints are called directly and their JSON is returned as plain
dicts; redditwarp's model loaders and its RateLimited handler are not used
(the loaders break on current API responses, and the handler can sleep for up
to 600 s under a lock).

Nothing here touches the network or the environment at import time. The HTTP
client and the configuration are created on the first request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Any, Mapping

from . import __version__

log = logging.getLogger("reddit_research_mcp")

PROJECT_URL = "https://github.com/jordanallenlewis/reddit-research-mcp"
DEFAULT_USER_AGENT = f"reddit-research-mcp/{__version__} (+{PROJECT_URL})"
REQUEST_TIMEOUT = 15.0  # seconds for the first attempt of an HTTP request
RETRY_TIMEOUT = 10.0  # seconds for the single retry, so a hung request gives up within ~26 s
MAX_RETRY_WAIT = 15.0  # longest wait we accept before retrying a 429 or an exhausted window
RETRY_BACKOFF = 1.0  # pause before the single retry of a transport error or 5xx
MORECHILDREN_BATCH = 100  # Reddit's limit per /api/morechildren call
INFO_BATCH = 100  # Reddit's limit per /api/info call


# ---------------------------------------------------------------- errors


class RedditAPIError(Exception):
    """Base class. The message is always non-empty and says what to do next."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message or self.__class__.__name__

    def __str__(self) -> str:
        return self.message


class ConfigError(RedditAPIError):
    """Invalid combination of REDDIT_* environment variables."""


class AuthError(RedditAPIError):
    """Reddit refused to issue or accept an access token."""


class RateLimitedError(RedditAPIError):
    def __init__(self, retry_after: float, detail: str = "") -> None:
        self.retry_after = max(1, math.ceil(retry_after))
        msg = f"Reddit rate limit reached; retry after {self.retry_after} s."
        if detail:
            msg += f" {detail}"
        super().__init__(msg)


class TransportError(RedditAPIError):
    """No usable HTTP response (DNS, connect, timeout), after one retry."""


class HTTPError(RedditAPIError):
    """A non-2xx response without a Reddit error label."""

    def __init__(
        self,
        status: int,
        path: str,
        *,
        location: str = "",
        reason: str = "",
        html: bool = False,
    ) -> None:
        self.status = status
        self.path = path
        self.location = location
        self.reason = reason
        self.html = html
        try:
            phrase = HTTPStatus(status).phrase
        except ValueError:
            phrase = ""
        msg = f"Reddit returned HTTP {status} {phrase}".rstrip() + f" for {path}"
        if location:
            msg += f" (redirect to {location})"
        elif reason:
            msg += f" ({reason})"
        super().__init__(msg)


class RedditLabelError(RedditAPIError):
    """Reddit answered with a labelled error such as private, banned or WIKI_DISABLED."""

    def __init__(self, label: str, explanation: str = "", status: int | None = None) -> None:
        self.label = label
        self.explanation = explanation
        self.status = status
        msg = f"Reddit refused the request: {label}"
        if explanation:
            msg += f" ({explanation})"
        super().__init__(msg)


# ---------------------------------------------------------------- configuration


@dataclass(frozen=True)
class Config:
    mode: str  # "anonymous", "app" (client credentials) or "user" (refresh token)
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    user_agent: str = DEFAULT_USER_AGENT


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read REDDIT_* variables. Partial combinations raise ConfigError."""
    env = os.environ if env is None else env

    def get(name: str) -> str:
        return (env.get(name) or "").strip()

    cid = get("REDDIT_CLIENT_ID")
    secret = get("REDDIT_CLIENT_SECRET")
    refresh = get("REDDIT_REFRESH_TOKEN")
    ua = get("REDDIT_USER_AGENT")

    if cid and secret:
        mode = "user" if refresh else "app"
    elif not cid and not secret:
        if refresh:
            raise ConfigError(
                "REDDIT_REFRESH_TOKEN is set without REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET. "
                "A refresh token only works together with the app that issued it: set all three, "
                "or unset REDDIT_REFRESH_TOKEN to use anonymous access."
            )
        mode = "anonymous"
    elif cid:
        raise ConfigError(
            "REDDIT_CLIENT_ID is set but REDDIT_CLIENT_SECRET is not. Set both (from a 'script' "
            "or 'web' app at https://www.reddit.com/prefs/apps), or unset REDDIT_CLIENT_ID to use "
            "anonymous access."
        )
    else:
        raise ConfigError(
            "REDDIT_CLIENT_SECRET is set but REDDIT_CLIENT_ID is not. Set both, or unset "
            "REDDIT_CLIENT_SECRET to use anonymous access."
        )
    if ua and (len(ua) > 256 or "\n" in ua or "\r" in ua):
        raise ConfigError("REDDIT_USER_AGENT must be a single line of at most 256 characters.")
    return Config(mode, cid, secret, refresh, ua or DEFAULT_USER_AGENT)


def _build_http(cfg: Config) -> Any:
    """Assemble redditwarp's OAuth + httpx stack without its RateLimited handler."""
    import httpx
    from redditwarp.auth import grants
    from redditwarp.core import grants as core_grants
    from redditwarp.core.authorizer_ASYNC import Authorized, Authorizer
    from redditwarp.core.const import TOKEN_OBTAINMENT_URL, TRUSTED_ORIGINS
    from redditwarp.core.direct_by_origin_ASYNC import DirectByOrigin
    from redditwarp.core.http_client_ASYNC import HTTPClient, RedditHTTPClient
    from redditwarp.core.reddit_please_send_json_ASYNC import RedditPleaseSendJSON
    from redditwarp.core.reddit_token_obtainment_client_ASYNC import RedditTokenObtainmentClient
    from redditwarp.http.misc_handlers.apply_params_and_headers_ASYNC import ApplyDefaultHeaders
    from redditwarp.http.transport.impls.httpx_ASYNC import HttpxConnector
    from redditwarp.http.util.case_insensitive_dict import CaseInsensitiveDict
    from redditwarp.util.redditwarp_installed_client_credentials import (
        get_device_id,
        get_redditwarp_client_id,
    )

    if cfg.mode == "anonymous":
        creds = (get_redditwarp_client_id(), "")
        grant: Any = core_grants.InstalledClientGrant(get_device_id())
    elif cfg.mode == "app":
        creds = (cfg.client_id, cfg.client_secret)
        grant = grants.ClientCredentialsGrant()
    else:
        creds = (cfg.client_id, cfg.client_secret)
        grant = grants.RefreshTokenGrant(cfg.refresh_token)

    connector = HttpxConnector(httpx.AsyncClient())
    headers = CaseInsensitiveDict({"User-Agent": cfg.user_agent})
    token_http = HTTPClient(ApplyDefaultHeaders(connector, headers))
    token_http.timeout = REQUEST_TIMEOUT
    authorizer = Authorizer(
        RedditTokenObtainmentClient(token_http, TOKEN_OBTAINMENT_URL, creds, grant)
    )
    api = RedditPleaseSendJSON(Authorized(connector, authorizer))
    http = RedditHTTPClient(
        DirectByOrigin(connector, {origin: api for origin in TRUSTED_ORIGINS}),
        headers=headers,
        authorizer=authorizer,
    )
    http.timeout = REQUEST_TIMEOUT
    http.user_agent_base = cfg.user_agent
    return http


# ---------------------------------------------------------------- helpers


def _header(headers: Mapping[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    try:
        v = headers.get(name)
    except Exception:
        v = None
    if v is None:
        low = name.lower()
        for k, val in headers.items():
            if k.lower() == low:
                return str(val)
        return ""
    return str(v)


def describe_exception(exc: BaseException) -> str:
    """Never-empty description: redditwarp raises transport errors with no message."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    parts: list[str] = []
    while cur is not None and id(cur) not in seen and len(parts) < 3:
        seen.add(id(cur))
        text = str(cur).strip()
        parts.append(f"{type(cur).__name__}: {text}" if text else type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
    return " <- ".join(parts)


def _parse_retry_after(headers: Mapping[str, str], now_wall: float | None = None) -> float:
    ra = _header(headers, "retry-after").strip()
    if ra:
        try:
            return max(0.0, float(ra))
        except ValueError:
            try:
                dt = parsedate_to_datetime(ra)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                now = now_wall if now_wall is not None else time.time()
                return max(0.0, dt.timestamp() - now)
            except (TypeError, ValueError, IndexError):
                pass
    reset = _header(headers, "x-ratelimit-reset").strip()
    if reset:
        try:
            return max(0.0, float(reset))
        except ValueError:
            pass
    return 60.0


_NOT_JSON = object()


# ---------------------------------------------------------------- client


class RedditClient:
    """Thin async Reddit API client: one place for auth, limits, retries and errors."""

    def __init__(self, config: Config | None = None) -> None:
        self._config = config
        self._http: Any = None
        self._remaining: float | None = None
        self._reset_at = 0.0
        self._used: int | None = None
        self._morechildren_lock: asyncio.Lock | None = None
        self.timeout = REQUEST_TIMEOUT
        self.retry_timeout = RETRY_TIMEOUT
        self.request_count = 0
        self.sleep = asyncio.sleep
        self.clock = time.monotonic

    # -- lifecycle

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config()
        return self._config

    def _ensure_http(self) -> Any:
        if self._http is None:
            self._http = _build_http(self.config)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            try:
                await self._http.close()
            finally:
                self._http = None

    # -- rate limit bookkeeping

    def rate_limit_status(self) -> dict[str, Any]:
        wait = max(0.0, self._reset_at - self.clock()) if self._remaining is not None else None
        return {"remaining": self._remaining, "reset_in": wait, "used": self._used}

    def _note_limits(self, headers: Mapping[str, str]) -> None:
        rem = _header(headers, "x-ratelimit-remaining")
        reset = _header(headers, "x-ratelimit-reset")
        used = _header(headers, "x-ratelimit-used")
        try:
            if rem and reset:
                self._remaining = float(rem)
                self._reset_at = self.clock() + float(reset)
            if used:
                self._used = int(float(used))
        except ValueError:
            pass

    async def _gate(self) -> None:
        """Spend one request from the window, waiting briefly or failing fast when it is empty."""
        if self._remaining is None:
            return
        now = self.clock()
        if now >= self._reset_at:
            self._remaining = None
            return
        if self._remaining < 1:
            wait = self._reset_at - now
            if wait <= MAX_RETRY_WAIT:
                await self.sleep(wait)
                self._remaining = None
                return
            raise RateLimitedError(
                wait,
                "The request window is used up (Reddit allows about 100 requests per minute "
                "per client; anonymous clients share a quota).",
            )
        self._remaining -= 1

    # -- transport

    async def _send_raw(
        self,
        verb: str,
        path: str,
        params: Mapping[str, str],
        data: Mapping[str, str] | None,
        timeout: float | None = None,
    ) -> tuple[int, Mapping[str, str], bytes]:
        """The single network seam (tests replace this)."""
        http = self._ensure_http()
        resp = await http.request(
            verb, path, params=dict(params), data=data, timeout=timeout or self.timeout
        )
        return resp.status, resp.headers, resp.data

    def _auth_failure(self, exc: BaseException) -> RedditAPIError:
        from redditwarp.http.exceptions import StatusCodeException

        detail = describe_exception(exc)
        mode = self._config.mode if self._config else "anonymous"
        status = getattr(exc, "status_code", None)
        if isinstance(exc, StatusCodeException) and status == 429:
            return RateLimitedError(60, "Reddit's token endpoint is rate limiting this client.")
        if isinstance(exc, StatusCodeException) and status is not None and status >= 500:
            return TransportError(
                f"Reddit's token endpoint returned HTTP {status}; Reddit may be having problems, "
                "retry in a minute."
            )
        if mode == "anonymous":
            return AuthError(
                f"Could not get an anonymous Reddit access token ({detail}). Retry shortly; if it "
                "keeps failing, create a Reddit app and set REDDIT_CLIENT_ID and "
                "REDDIT_CLIENT_SECRET (see the README)."
            )
        if mode == "user":
            return AuthError(
                f"Reddit rejected the credentials ({detail}). Check REDDIT_CLIENT_ID, "
                "REDDIT_CLIENT_SECRET and REDDIT_REFRESH_TOKEN (the token must come from the same "
                "app), or unset all three for anonymous access."
            )
        return AuthError(
            f"Reddit rejected REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET ({detail}). Check the app at "
            "https://www.reddit.com/prefs/apps (a 'script' or 'web' app), or unset both for "
            "anonymous access."
        )

    async def request(
        self,
        verb: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> Any:
        """Send one API request and return decoded JSON.

        Retries once on a transport error or 5xx (after RETRY_BACKOFF s) and once
        on a 429 whose Retry-After is at most MAX_RETRY_WAIT s. Never sleeps longer.
        The retry uses the shorter retry_timeout, so a hung network costs at most
        timeout + RETRY_BACKOFF + retry_timeout seconds (26 s by default).
        """
        from redditwarp.auth.exceptions import OAuth2ResponseError, UnknownTokenType
        from redditwarp.core.exceptions import AuthError as RWAuthError
        from redditwarp.core.exceptions import CredentialsError
        from redditwarp.http.exceptions import StatusCodeException, TimeoutException
        from redditwarp.http.exceptions import TransportError as RWTransportError

        q = {k: str(v) for k, v in (params or {}).items() if v is not None and v != ""}
        q.setdefault("raw_json", "1")
        form = None if data is None else {k: str(v) for k, v in data.items() if v is not None}

        for attempt in (0, 1):
            await self._gate()
            started = self.clock()
            self.request_count += 1
            limit = self.timeout if attempt == 0 else min(self.timeout, self.retry_timeout)
            try:
                status, headers, body = await self._send_raw(verb, path, q, form, timeout=limit)
            except RedditAPIError:
                raise
            except TimeoutException as exc:
                log.debug("%s %s timeout after %.1fs", verb, path, self.clock() - started)
                if attempt == 0:
                    await self.sleep(RETRY_BACKOFF)
                    continue
                raise TransportError(
                    f"Reddit did not respond for {verb} {path} (waited {self.timeout:.0f} s, then "
                    f"{limit:.0f} s on a retry). Retry later, or ask for less (smaller limit)."
                ) from exc
            except RWTransportError as exc:
                log.debug("%s %s transport error %s", verb, path, describe_exception(exc))
                if attempt == 0:
                    await self.sleep(RETRY_BACKOFF)
                    continue
                raise TransportError(
                    f"Network error reaching Reddit for {verb} {path} "
                    f"({describe_exception(exc.__cause__ or exc)}), tried twice. Check the "
                    "network connection and retry."
                ) from exc
            except (CredentialsError, RWAuthError, OAuth2ResponseError, UnknownTokenType) as exc:
                raise self._auth_failure(exc) from exc
            except StatusCodeException as exc:
                # Only the token endpoint raises this; API responses are inspected below.
                raise self._auth_failure(exc) from exc
            except Exception as exc:  # pragma: no cover - defensive
                raise RedditAPIError(
                    f"Unexpected error calling Reddit for {verb} {path}: {describe_exception(exc)}"
                ) from exc

            self._note_limits(headers)
            log.debug(
                "%s %s -> %s %.2fs %dB remaining=%s",
                verb,
                path,
                status,
                self.clock() - started,
                len(body or b""),
                _header(headers, "x-ratelimit-remaining") or "?",
            )
            if status == 429:
                wait = _parse_retry_after(headers)
                if attempt == 0 and wait <= MAX_RETRY_WAIT:
                    await self.sleep(max(wait, 0.5))
                    self._remaining = None  # waited as asked; let the retry through the gate
                    continue
                raise RateLimitedError(wait)
            if status >= 500 and attempt == 0:
                await self.sleep(RETRY_BACKOFF)
                continue
            return self._decode(status, headers, body, path)
        raise RedditAPIError(f"Request to {path} failed after a retry")  # pragma: no cover

    def _decode(self, status: int, headers: Mapping[str, str], body: bytes, path: str) -> Any:
        ctype = _header(headers, "content-type")
        parsed: Any = None
        if body:
            try:
                parsed = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                parsed = _NOT_JSON
        if 200 <= status < 300:
            if parsed is _NOT_JSON:
                raise RedditAPIError(
                    f"Reddit returned a non-JSON response for {path} "
                    f"(HTTP {status}, {ctype or 'no content type'}); retry, and report it if "
                    "it persists."
                )
            self._raise_for_label(parsed, status)
            return parsed
        if 300 <= status < 400:
            raise HTTPError(status, path, location=_header(headers, "location"))
        if isinstance(parsed, Mapping):
            self._raise_for_label(parsed, status)
            reason = parsed.get("message") or parsed.get("reason") or ""
            raise HTTPError(status, path, reason=str(reason))
        raise HTTPError(status, path, html=ctype.startswith("text/html"))

    @staticmethod
    def _raise_for_label(parsed: Any, status: int) -> None:
        from redditwarp.exceptions import RedditError, raise_for_reddit_error

        if not isinstance(parsed, Mapping):
            return
        try:
            raise_for_reddit_error(parsed)
        except RedditError as exc:
            raise RedditLabelError(exc.label, exc.explanation, status) from exc

    # -- endpoint helpers

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def morechildren(self, post_id: str, ids: list[str], sort: str) -> list[dict]:
        """Expand up to 100 comment ids. Reddit allows one call at a time per client."""
        if len(ids) > MORECHILDREN_BATCH:
            raise ValueError(f"at most {MORECHILDREN_BATCH} ids per morechildren call")
        if self._morechildren_lock is None:
            self._morechildren_lock = asyncio.Lock()
        async with self._morechildren_lock:
            root = await self.request(
                "GET",
                "/api/morechildren",
                params={
                    "link_id": "t3_" + post_id,
                    "children": ",".join(ids),
                    "sort": sort,
                    "api_type": "json",
                    "limit_children": "false",
                },
            )
        return list((((root or {}).get("json") or {}).get("data") or {}).get("things") or [])
