"""LLM chat client (OpenAI-compatible /chat/completions). Optional: everything works without it.

Set MAAS_BASE_URL (ending in /v1), MAAS_API_KEY, MAAS_MODEL. The names are historical: any
OpenAI-compatible server works, e.g. Chula's LiteLLM proxy (https://llm.nattee.net/v1, model qwen3.5).
Use a standard chat model, not a reasoning one (CLAUDE.md §3 budget); for Qwen on vLLM/LiteLLM set
LLM_NO_THINKING=1 to send chat_template_kwargs.enable_thinking=false.

Any failure (no key, daily quota used up, expired key, timeout) returns None, and callers fall back
to the rule-based path.
"""
import json
import os
import re

import httpx


def available() -> bool:
    return bool(os.environ.get("MAAS_API_KEY") and os.environ.get("MAAS_BASE_URL") and os.environ.get("MAAS_MODEL"))


THINK = re.compile(r"<think>.*?</think>\s*", re.S)


def payload(messages: list[dict], temperature: float, max_tokens: int) -> dict:
    body = {"model": os.environ["MAAS_MODEL"], "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    if os.environ.get("LLM_NO_THINKING") == "1":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body


def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 600, timeout: float = 20) -> str | None:
    if not available():
        return None
    try:
        r = httpx.post(f"{os.environ['MAAS_BASE_URL'].rstrip('/')}/chat/completions", timeout=timeout,
                       headers={"Authorization": f"Bearer {os.environ['MAAS_API_KEY']}"},
                       json=payload(messages, temperature, max_tokens))
        if r.status_code == 429:
            print("llm error: quota or rate limit reached (429); using rules", flush=True)
            return None
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"] or ""
        # Some Qwen templates still emit an (empty) think block with thinking off.
        return THINK.sub("", content).strip() or None
    except Exception as e:
        print(f"llm error: {e!r}", flush=True)
        return None


def chat_json(system: str, user: str) -> dict | None:
    """Zero-shot JSON extraction. Returns None if the model is off or the output isn't JSON."""
    out = chat([{"role": "system", "content": system}, {"role": "user", "content": user}], temperature=0)
    if not out:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


NUM = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_preserved(draft: str, rewritten: str) -> bool:
    """Rule 1 guard: the LLM may rephrase, but every number it writes must come from the draft."""
    allowed = set(NUM.findall(draft))
    return all(n in allowed for n in NUM.findall(rewritten))
