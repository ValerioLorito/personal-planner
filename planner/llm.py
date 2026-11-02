"""
llm.py — OpenAI-compatible chat client with a daily request budget.

OpenRouter's free tier allows ~50 requests/day, so every call is counted in
`llm_calls` and refused (LLMUnavailable) once the day's budget is spent. The
calendar watcher has its own, smaller sub-budget so background reactions can
never starve the user's own requests.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date

from .config import DATA_DIR, expand
from .db import now_iso


class LLMUnavailable(Exception):
    """Not configured, out of budget, or the endpoint failed."""


class LLM:
    def __init__(self, cfg: dict, db):
        self.cfg = cfg
        self.db = db

    # ---- configuration ----
    @property
    def c(self) -> dict:
        return self.cfg["llm"]

    def api_key(self) -> str:
        env = self.c.get("api_key_env") or ""
        if env and os.environ.get(env):
            return os.environ[env].strip()
        # Fallback env checks depending on provider / base_url
        if "googleapis.com" in (self.c.get("base_url") or ""):
            for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
                if os.environ.get(k):
                    return os.environ[k].strip()
        else:
            if os.environ.get("OPENROUTER_API_KEY"):
                return os.environ["OPENROUTER_API_KEY"].strip()
        path = expand(self.c.get("key_file") or "") or os.path.join(DATA_DIR, "llm_key")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        return ""

    def ready(self) -> bool:
        return bool(self.c.get("base_url") and self.c.get("model") and
                    (self.api_key() or not self.c.get("api_key_env")))

    # ---- budget ----
    def used_today(self, purpose_prefix: str = "") -> int:
        today = date.today().isoformat()
        if purpose_prefix:
            row = self.db.one("SELECT COUNT(*) n FROM llm_calls WHERE day=? AND purpose LIKE ?",
                              (today, purpose_prefix + "%"))
        else:
            row = self.db.one("SELECT COUNT(*) n FROM llm_calls WHERE day=?", (today,))
        return int(row["n"]) if row else 0

    def budget(self) -> dict:
        used = self.used_today()
        watch = self.used_today("watch")
        is_gemini = "googleapis" in (self.c.get("base_url") or "")
        default_daily = 1500 if is_gemini else 50
        default_watch = 300 if is_gemini else 12
        limit = int(self.c.get("daily_budget", default_daily))
        watch_limit = int(self.c.get("watch_budget", default_watch))
        provider = self.c.get("provider", "gemini" if is_gemini else ("openrouter" if "openrouter" in (self.c.get("base_url") or "") else "custom"))
        return {
            "used": used,
            "limit": limit,
            "watch_used": watch,
            "watch_limit": watch_limit,
            "remaining": max(0, limit - used),
            "ready": self.ready(),
            "model": self.c.get("model", ""),
            "provider": provider,
            "base_url": self.c.get("base_url", ""),
            "has_key": bool(self.api_key()),
        }

    def can_call(self, purpose: str) -> bool:
        if purpose == "test":
            return True
        b = self.budget()
        if not b["ready"] or b["remaining"] <= 0:
            return False
        if purpose.startswith("watch") and b["watch_used"] >= b["watch_limit"]:
            return False
        return True

    def test_connection(self) -> dict:
        """Verify endpoint and credentials with a tiny test call."""
        if not self.ready():
            return {"ok": False, "error": "LLM not configured (missing model, URL, or API key)"}
        try:
            reply = self.chat([{"role": "user", "content": "Respond with the single word: OK"}],
                              purpose="test", max_tokens=250)
            return {"ok": True, "reply": reply.strip(), "model": self.c.get("model", "")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- the call ----
    def chat(self, messages: list, purpose: str, max_tokens: int = 4500) -> str:
        if not self.ready():
            raise LLMUnavailable("LLM not configured (base_url/model/key)")
        if not self.can_call(purpose):
            b = self.budget()
            raise LLMUnavailable("daily LLM budget exhausted (%d/%d used%s)" % (
                b["used"], b["limit"],
                ", watcher cap reached" if purpose.startswith("watch") else ""))
        url = self.c["base_url"].rstrip("/") + "/chat/completions"
        body = {"model": self.c["model"], "messages": messages,
                "temperature": self.c.get("temperature", 0.2), "max_tokens": max_tokens}
        # Cap reasoning tokens on OpenRouter so reasoning models don't starve content
        if "openrouter.ai" in url:
            body["reasoning"] = {"max_tokens": 800}

        headers = {"Content-Type": "application/json"}
        if "openrouter.ai" in url:
            headers["HTTP-Referer"] = "http://127.0.0.1/planner"
            headers["X-Title"] = "Planner"
        key = self.api_key()
        if key:
            headers["Authorization"] = "Bearer " + key
        last_err = ""
        for attempt in range(2):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.c.get("timeout", 120)) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
                if payload.get("error"):
                    err_val = payload["error"]
                    msg = err_val.get("message", err_val) if isinstance(err_val, dict) else str(err_val)
                    raise LLMUnavailable(str(msg)[:300])
                choice = (payload.get("choices") or [{}])[0]
                msg = choice.get("message", {})
                text = msg.get("content") or ""
                # If content is empty but model produced reasoning, check if reasoning has the text/JSON
                if not text.strip() and msg.get("reasoning"):
                    text = msg["reasoning"]
                model_name = payload.get("model", "")
                self._record(purpose, True, model_name, "")
                if not text.strip():
                    finish = choice.get("finish_reason", "unknown")
                    raise LLMUnavailable("empty completion (finish_reason: %s) from %s" % (finish, model_name or "model"))
                return strip_think(text)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                msg = detail
                try:
                    p = json.loads(detail)
                    if isinstance(p, list) and p and isinstance(p[0], dict):
                        p = p[0]
                    if isinstance(p, dict) and "error" in p:
                        e_val = p["error"]
                        msg = e_val.get("message", detail) if isinstance(e_val, dict) else str(e_val)
                except Exception:
                    pass
                last_err = "HTTP %d: %s" % (e.code, msg)
                self._record(purpose, False, "", last_err)
                if e.code in (429, 502, 503) and attempt == 0:
                    time.sleep(4)
                    continue
                break
            except LLMUnavailable as e:
                last_err = str(e)
                self._record(purpose, False, "", last_err)
                break
            except Exception as e:  # network, timeout, bad JSON
                last_err = "%s: %s" % (type(e).__name__, e)
                self._record(purpose, False, "", last_err)
                break
        raise LLMUnavailable(last_err or "LLM call failed")

    def _record(self, purpose, ok, model, error):
        self.db.insert("llm_calls", {"ts": now_iso(), "day": date.today().isoformat(),
                                     "purpose": purpose, "ok": 1 if ok else 0,
                                     "model": model, "error": error[:500]})


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _try_repair_truncated_json(s: str) -> dict | None:
    s = s.strip()
    if not s.startswith("{"):
        return None
    in_str, esc = False, False
    stack = []
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in ('{', '['):
                stack.append('}' if ch == '{' else ']')
            elif ch in ('}', ']') and stack:
                if stack[-1] == ch:
                    stack.pop()
    repair = s
    if in_str:
        repair += '"'
    repair = re.sub(r',\s*$', '', repair)
    while stack:
        repair += stack.pop()
    try:
        obj = json.loads(repair)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    return None


def extract_json(text: str):
    """First balanced JSON object in `text` (handles ```json fences, prose, and truncated objects)."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
        start = text.find("{", start + 1) if not candidates else -1
    for cand in candidates:
        for attempt in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                obj = json.loads(attempt)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                continue

    # Attempt repair if cut off before closing
    if "{" in text:
        start_idx = text.find("{")
        repaired = _try_repair_truncated_json(text[start_idx:])
        if repaired:
            return repaired

    return None
