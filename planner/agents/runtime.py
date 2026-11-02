"""
runtime.py — a small, model-agnostic agent loop.

Free OpenRouter models do not reliably support native tool calling, so the
protocol is plain JSON in the message text:

    {"thought": "...", "actions": [{"tool": "read_file", "args": {...}}, ...]}
    {"thought": "...", "final": {...}}

Several actions per turn are encouraged (they all run before the next call),
because every turn costs one request of the daily budget. The last allowed
turn is announced so the model always lands a `final`.
"""
from __future__ import annotations

import json
import time

from ..llm import LLMUnavailable, extract_json

RESULT_CAP = 3500        # chars per tool result
TURN_CAP = 10000         # chars of tool results per turn
MAX_ACTIONS = 5


class AgentError(Exception):
    pass


class Tool:
    def __init__(self, name: str, signature: str, description: str, fn):
        self.name = name
        self.signature = signature
        self.description = description
        self.fn = fn


class Agent:
    def __init__(self, llm, purpose: str, system: str, tools: list, max_steps: int = 4,
                 on_trace=None, looks_final=None):
        self.llm = llm
        self.purpose = purpose
        self.tools = {t.name: t for t in tools}
        self.max_steps = max(1, max_steps)
        self.on_trace = on_trace or (lambda step: None)
        self.looks_final = looks_final or (lambda obj: False)
        self.trace = []
        self.system = system.strip() + "\n\n" + self._protocol()

    def _protocol(self) -> str:
        lines = ["## Response protocol (strict)",
                 "Reply with exactly ONE JSON object and nothing else (no markdown preamble, no prose outside JSON).",
                 "Keep all string fields (thought, summary, rationale, notes) concise (under 3 sentences).",
                 "When you have enough information, answer: {\"thought\": \"<1-2 sentences>\", \"final\": { ... }}"]
        if self.tools:
            lines += [
                "To gather information first: {\"thought\": \"<what you need and why>\", \"actions\": "
                "[{\"tool\": \"<name>\", \"args\": {...}}]}",
                "Up to %d actions per turn — batch them, results arrive together in the next message." % MAX_ACTIONS,
                "You have at most %d turns in total (each turn is expensive). Do not re-request what you already have."
                % self.max_steps,
                "", "## Tools"]
            for t in self.tools.values():
                lines.append("- %s — %s" % (t.signature, t.description))
        else:
            lines.append("Answer in a single turn.")
        return "\n".join(lines)

    def _emit(self, step: dict) -> None:
        step["ts"] = time.strftime("%H:%M:%S")
        self.trace.append(step)
        try:
            self.on_trace(step)
        except Exception:
            pass

    def run(self, user: str) -> dict:
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": user}]
        for turn in range(1, self.max_steps + 1):
            last = turn == self.max_steps
            if last and turn > 1:
                messages[-1]["content"] += ("\n\nThis is your FINAL turn: reply now with "
                                            "{\"thought\": ..., \"final\": {...}} using what you know.")
            self._emit({"type": "thinking", "turn": turn})
            try:
                text = self.llm.chat(messages, purpose=self.purpose)
            except LLMUnavailable as e:
                self._emit({"type": "error", "text": str(e)})
                raise AgentError(str(e))
            obj = extract_json(text)
            if obj is None:
                self._emit({"type": "error", "text": "reply was not JSON: %s" % text[:200]})
                if last:
                    raise AgentError("the model did not return JSON")
                messages.append({"role": "assistant", "content": text[:4000]})
                if "{" in text and not text.rstrip().endswith("}"):
                    hint = ("Your previous reply was cut off mid-output. Reply NOW with the COMPLETE, compact JSON object directly. "
                            "Keep strings and rationale short so it fits comfortably.")
                else:
                    hint = "That was not valid JSON. Please reply with a single valid JSON object following the protocol."
                messages.append({"role": "user", "content": hint})
                continue
            thought = str(obj.get("thought", ""))[:600]
            if "final" in obj and isinstance(obj["final"], dict):
                self._emit({"type": "final", "thought": thought})
                return obj["final"]
            actions = obj.get("actions") or []
            if not actions and self.looks_final(obj):
                self._emit({"type": "final", "thought": thought})
                return obj
            if not isinstance(actions, list) or not actions:
                if last:
                    raise AgentError("the model ended without a final answer")
                messages.append({"role": "assistant", "content": json.dumps(obj)[:4000]})
                messages.append({"role": "user", "content": "Reply with either actions or final."})
                continue
            self._emit({"type": "actions", "thought": thought,
                        "actions": [{"tool": a.get("tool"), "args": a.get("args", {})}
                                    for a in actions[:MAX_ACTIONS] if isinstance(a, dict)]})
            results, used = [], 0
            for a in actions[:MAX_ACTIONS]:
                if not isinstance(a, dict):
                    continue
                name, args = a.get("tool"), a.get("args") or {}
                tool = self.tools.get(name)
                if tool is None:
                    out = "ERROR: unknown tool %r" % name
                else:
                    try:
                        out = str(tool.fn(**args) if isinstance(args, dict) else tool.fn(args))
                    except TypeError as e:
                        out = "ERROR: bad arguments for %s: %s (signature %s)" % (name, e, tool.signature)
                    except Exception as e:
                        out = "ERROR: %s" % e
                if len(out) > RESULT_CAP:
                    out = out[:RESULT_CAP] + "\n… [result cut at %d chars]" % RESULT_CAP
                if used + len(out) > TURN_CAP:
                    out = out[:max(200, TURN_CAP - used)] + "\n… [turn budget reached]"
                used += len(out)
                results.append("### %s %s\n%s" % (name, json.dumps(args, ensure_ascii=False)[:200], out))
                self._emit({"type": "result", "tool": name, "args": args,
                            "preview": out[:240], "ok": not out.startswith("ERROR")})
            messages.append({"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)[:4000]})
            messages.append({"role": "user", "content": "TOOL RESULTS (turn %d of %d):\n\n%s" % (
                turn, self.max_steps, "\n\n".join(results))})
        raise AgentError("ran out of turns")
