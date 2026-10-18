"""
LLM client for any OpenAI-compatible provider (POST {base_url}/chat/completions).
Standard library only (urllib.request), no SDK.

Settings come from config.settings (llm_api_key, llm_base_url, llm_model, llm_timeout_s,
llm_max_retries, llm_price_in/out, llm_max_cost_per_run). The client does not add any
system prompt of its own: the caller passes it (prompts/system.txt.j2 rendered over the profile).

One client instance = one generation run:
- retries on 429/5xx/timeouts/network errors (2, 4, 8 s + jitter, Retry-After respected),
  no retry on 400/401/403 (wrong key or model);
- cache of answers in data/llm_cache/<key[:2]>/<key>.json, a repeated identical request costs 0;
  the key covers the request and its scope (campaign, artifact, platform from `meta`), so two
  campaigns never share an answer; a caller that checks answers passes store=False and calls
  store() only for an answer that passed the checks;
- every call is a line in data/llm_usage.jsonl; the run stops with BudgetExceeded before a call
  that may exceed LLM_MAX_COST_PER_RUN (counted only when the provider returns usage.cost or
  LLM_PRICE_* are set; otherwise a warning, once per run).
The API key is never logged and is scrubbed from provider error texts.

BASE_SYSTEM_PROMPT is kept for compatibility: it is the prompts/system.txt.j2 render over the
installation profile (computed on access, the text lives in templates, not here).
"""
import hashlib
import http.client
import json
import logging
import os
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import DEFAULT_LLM_BASE_URL as DEFAULT_BASE_URL, DEFAULT_LLM_MODEL as DEFAULT_MODEL, load_settings

logger = logging.getLogger("LLMClient")

CACHE_SCHEMA_VERSION = 2  # 2: scope in the key, only checked answers stored
CACHE_SCOPE = ("campaign_id", "artifact", "platform")
RETRY_STATUSES = {429, 500, 502, 503, 504}
NO_RETRY_STATUSES = {400, 401, 403}
MAX_RETRY_AFTER_S = 300
ERROR_BODY_LIMIT = 500


class LLMError(RuntimeError):
    """Provider call failed (after retries, or not retriable)."""


class LLMNotConfigured(LLMError):
    """No API key: mode llm cannot run."""


class LLMAuthError(LLMError):
    """400/401/403: the key or the model is wrong — retrying will not help."""


class BudgetExceeded(LLMError):
    """The next call may exceed LLM_MAX_COST_PER_RUN; the run must stop."""


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    cost: Optional[Decimal]  # None — provider returned no cost and prices are not set
    cached: bool
    attempt: int = 0  # HTTP attempts made (0 for a cache hit)
    key: Optional[str] = None  # cache key of the request


def _data_dir() -> Path:
    from config import settings as settings_module
    return settings_module.DATA_DIR


def cache_key(model: str, messages: List[Dict[str, str]], temperature: float, max_tokens: int,
              meta: Optional[Dict[str, Any]] = None) -> str:
    scope = {name: (meta or {}).get(name) for name in CACHE_SCOPE}
    raw = json.dumps([CACHE_SCHEMA_VERSION, model, messages, temperature, max_tokens, scope],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)


class LLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        *,
        settings: Any = None,
        cache_dir: Optional[Path] = None,
        usage_path: Optional[Path] = None,
        refresh_cache: bool = False,
        use_cache: bool = True,
    ):
        settings = settings or load_settings()
        self.api_key = api_key or settings.llm_api_key.get_secret_value()
        self.base_url = (base_url or settings.llm_base_url or DEFAULT_BASE_URL).rstrip("/")
        self.default_model = default_model or settings.llm_model or DEFAULT_MODEL
        self.timeout = settings.llm_timeout_s
        self.max_retries = settings.llm_max_retries
        self.price_in = settings.llm_price_in
        self.price_out = settings.llm_price_out
        self.max_cost_per_run = settings.llm_max_cost_per_run
        data_dir = _data_dir()
        self.cache_dir = Path(cache_dir) if cache_dir else data_dir / "llm_cache"
        self.usage_path = Path(usage_path) if usage_path else data_dir / "llm_usage.jsonl"
        self.refresh_cache = refresh_cache
        self.use_cache = use_cache
        self.spent = Decimal(0)  # cost of this run (known costs only)
        self.calls = 0  # HTTP requests sent by this client
        self._warned_no_cost = False
        self._sleep = time.sleep

    def is_configured(self) -> bool:
        return bool(self.api_key and len(self.api_key) > 10)

    # --- cost -------------------------------------------------------------

    def price_cost(self, prompt_tokens: Optional[int], completion_tokens: Optional[int]) -> Optional[Decimal]:
        if self.price_in is None or self.price_out is None or prompt_tokens is None or completion_tokens is None:
            return None
        return (Decimal(prompt_tokens) * self.price_in + Decimal(completion_tokens) * self.price_out) / Decimal(1_000_000)

    def estimate_cost(self, max_tokens: int) -> Optional[Decimal]:
        """Upper bound of one call by output tokens (input is not counted)."""
        if self.price_out is None:
            return None
        return Decimal(max_tokens) * self.price_out / Decimal(1_000_000)

    def _check_budget(self, max_tokens: int) -> None:
        if self.max_cost_per_run is None:
            return
        if self.max_cost_per_run <= 0:
            # also the value an unparseable or negative LLM_MAX_COST_PER_RUN falls back to
            raise BudgetExceeded("LLM_MAX_COST_PER_RUN=0: LLM calls are refused; set a positive budget "
                                 "(or empty / none for no limit)")
        estimate = self.estimate_cost(max_tokens) or Decimal(0)
        if self.spent + estimate > self.max_cost_per_run:
            raise BudgetExceeded(
                f"LLM_MAX_COST_PER_RUN={self.max_cost_per_run}: spent {self.spent}, "
                f"next call may cost up to {estimate}"
            )

    # --- cache & usage ----------------------------------------------------

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _read_cache(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.use_cache or self.refresh_cache:
            return None
        path = self._cache_path(key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"LLM cache entry {path.name} unreadable, ignored: {e}")
            return None

    def _write_cache(self, key: str, entry: Dict[str, Any]) -> None:
        if not self.use_cache:
            return
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def _log_usage(self, result: LLMResult, meta: Optional[Dict[str, Any]]) -> None:
        meta = meta or {}
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "campaign_id": meta.get("campaign_id"),
            "artifact": meta.get("artifact"),
            "platform": meta.get("platform"),
            "model": result.model,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost": float(result.cost) if result.cost is not None else None,
            "cached": result.cached,
            "attempt": result.attempt,
        }
        try:
            self.usage_path.parent.mkdir(parents=True, exist_ok=True)
            with self.usage_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning(f"Cannot write {self.usage_path}: {e}")

    # --- transport --------------------------------------------------------

    def _scrub(self, text: str) -> str:
        return text.replace(self.api_key, "***") if self.api_key else text

    def _post(self, payload: Dict[str, Any]) -> tuple:
        """Sends the request with retries; returns (response json, attempts)."""
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        attempts = 1 + self.max_retries
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "SERM-Distribution-Engine/1.0",
                },
            )
            delay = None
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8")), attempt
            except urllib.error.HTTPError as e:
                err_body = self._scrub(e.read().decode("utf-8", errors="ignore")[:ERROR_BODY_LIMIT])
                if e.code in NO_RETRY_STATUSES:
                    raise LLMAuthError(f"LLM provider rejected the request ({e.code}): check the API key and "
                                       f"the model '{payload['model']}'. {err_body}") from None
                if e.code not in RETRY_STATUSES:
                    raise LLMError(f"LLM provider error ({e.code}): {err_body}") from None
                logger.warning(f"LLM HTTP {e.code}, attempt {attempt}/{attempts}")
                delay = _retry_after_seconds(e.headers.get("Retry-After") if e.headers else None)
                last_error = f"HTTP {e.code}: {err_body}"
            except (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError,
                    ConnectionError) as e:
                logger.warning(f"LLM request failed ({type(e).__name__}), attempt {attempt}/{attempts}")
                last_error = self._scrub(str(e))
            except ValueError as e:
                raise LLMError(f"LLM provider returned invalid JSON: {e}") from None
            if attempt < attempts:
                if delay is None:
                    delay = 2 ** attempt + random.uniform(0, 1)
                self._sleep(delay)
        raise LLMError(f"LLM provider unavailable after {attempts} attempts: {last_error}")

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        meta: Optional[Dict[str, Any]] = None,
        store: bool = True,
    ) -> LLMResult:
        """
        One chat completion. `meta` (campaign_id, artifact, platform) goes to llm_usage.jsonl and
        into the cache key. store=False: the answer is not cached here (see store()).
        """
        target_model = model or self.default_model
        key = cache_key(target_model, messages, temperature, max_tokens, meta)

        cached = self._read_cache(key)
        if cached is not None:
            usage = cached.get("usage") or {}
            result = LLMResult(
                text=cached.get("response", {}).get("text", ""),
                model=cached.get("response", {}).get("model") or target_model,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                cost=Decimal(0),
                cached=True,
                key=key,
            )
            self._log_usage(result, meta)
            return result

        if not self.is_configured():
            raise LLMNotConfigured("LLM API key is not configured (LLM_API_KEY / POLZA_API_KEY)")
        self._check_budget(max_tokens)

        payload = {"model": target_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        data, attempt = self._post(payload)
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if usage.get("cost") is not None:
            cost = Decimal(str(usage["cost"]))
        else:
            cost = self.price_cost(prompt_tokens, completion_tokens)
        if cost is not None:
            self.spent += cost
        elif self.max_cost_per_run is not None and not self._warned_no_cost:
            self._warned_no_cost = True
            logger.warning(f"LLM_MAX_COST_PER_RUN={self.max_cost_per_run} cannot be applied: the provider returned "
                           f"no usage.cost and LLM_PRICE_INPUT_PER_MTOK / LLM_PRICE_OUTPUT_PER_MTOK are not set")

        result = LLMResult(
            text=text,
            model=data.get("model") or target_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            cached=False,
            attempt=attempt,
            key=key,
        )
        logger.info(f"LLM {result.model}: {len(text)} chars, attempt {attempt}, cost {cost}")
        if store:
            self.store(key, result)
        self._log_usage(result, meta)
        return result

    def store(self, key: str, result: LLMResult) -> None:
        """Caches `result` under `key` (a key returned by complete(); the answer may be a later one)."""
        self._write_cache(key, {
            "response": {"text": result.text, "model": result.model},
            "usage": {"prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
                      "cost": str(result.cost) if result.cost is not None else None},
        })

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096
    ) -> str:
        """Legacy wrapper: text of one completion."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt.strip()})
        messages.append({"role": "user", "content": prompt})
        return self.complete(messages, model=model, temperature=temperature, max_tokens=max_tokens).text


# Old name, kept for imports
PolzaLLMClient = LLMClient


def __getattr__(name: str) -> str:
    # BASE_SYSTEM_PROMPT: rendered on access over the installation profile (owner: verbatim override)
    if name == "BASE_SYSTEM_PROMPT":
        from core.llm_generation import system_prompt
        from core.profile import get_profile
        return system_prompt(get_profile())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
