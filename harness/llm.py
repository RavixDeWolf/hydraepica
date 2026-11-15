"""llm.py - short chat completions for the parts that are not a head (the gateway's quick replies).

Endpoints come from config, tried in order: typically a local OpenAI-compatible server first (Ollama, LM Studio,
vLLM, llama.cpp) and a hosted router (OpenRouter) as the fallback. Every fallback and every give-up is logged,
with the reason, so a quiet degradation is still visible.

[[llm.endpoints]]
name = "local"
url = "http://127.0.0.1:11434/v1/chat/completions"
model = "llama3.1:8b"
[[llm.endpoints]]
name = "openrouter"
url = "https://openrouter.ai/api/v1/chat/completions"
model = "meta-llama/llama-3.1-8b-instruct"
key_env = "OPENROUTER_API_KEY"
"""
import json
import os
import urllib.request

import config

TIMEOUT_S = 30


def endpoints():
    return config.section("llm").get("endpoints") or []


def _call(ep, messages, opener=None):
    headers = {"Content-Type": "application/json"}
    if ep.get("key_env"):
        key = config.secret(ep["key_env"])
        if not key:
            raise RuntimeError(f"${ep['key_env']} is not set")
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps({"model": ep["model"], "messages": messages, "max_tokens": ep.get("max_tokens", 400)}).encode()
    req = urllib.request.Request(ep["url"], data=body, headers=headers)
    with (opener or urllib.request.urlopen)(req, timeout=ep.get("timeout", TIMEOUT_S)) as r:
        out = json.load(r)
    return out["choices"][0]["message"]["content"].strip()


def chat(text, system, log=print, opener=None):
    """-> the first endpoint's answer, or None when none answered (each failure logged, then the give-up)."""
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": text}]
    eps = endpoints()
    for i, ep in enumerate(eps):
        try:
            ans = _call(ep, msgs, opener=opener)
            if i:
                log(f"LLM answered by fallback {ep.get('name', i)} after {i} failure(s)")
            return ans
        except Exception as e:
            log(f"LLM endpoint {ep.get('name', i)} failed: {type(e).__name__}: {str(e)[:160]}")
    log(f"LLM gave up: {len(eps)} endpoint(s), none answered" if eps else "LLM: no [[llm.endpoints]] configured")
    return None
