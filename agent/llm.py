"""MaaS chat client (OpenAI-compatible /chat/completions). Optional: everything works without it.

Set MAAS_BASE_URL (ending in /v1), MAAS_API_KEY, MAAS_MODEL. Use a standard chat model, not a
reasoning one (CLAUDE.md §3 budget).
"""
import json
import os
import re

import httpx


def available() -> bool:
    return bool(os.environ.get("MAAS_API_KEY") and os.environ.get("MAAS_BASE_URL") and os.environ.get("MAAS_MODEL"))


def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 600, timeout: float = 20) -> str | None:
    if not available():
        return None
    try:
        r = httpx.post(f"{os.environ['MAAS_BASE_URL'].rstrip('/')}/chat/completions", timeout=timeout,
                       headers={"Authorization": f"Bearer {os.environ['MAAS_API_KEY']}"},
                       json={"model": os.environ["MAAS_MODEL"], "messages": messages,
                             "temperature": temperature, "max_tokens": max_tokens})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
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
