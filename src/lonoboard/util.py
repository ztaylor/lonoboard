"""Small shared helpers: running commands, HTTP, and streaming chat completions."""
import json
import shutil
import subprocess
import time
import unicodedata
import urllib.error
import urllib.request


def sh(*cmd, timeout=5, merge=False):
    """Run a command, return its stdout ('' if it's missing, fails to start or times out).
    merge=True includes stderr (interleaved), e.g. for `docker logs`."""
    if not shutil.which(cmd[0]):
        return ""
    try:
        if merge:
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  timeout=timeout).stdout
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def http(url, timeout=2, headers=None, data=None, method=None):
    """GET/POST url -> (status, body bytes); status 0 when unreachable."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""
        finally:
            e.close()
    except Exception:
        return 0, b""


def get_json(url, timeout=2, headers=None):
    status, body = http(url, timeout, headers)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def get_text(url, timeout=2, headers=None):
    status, body = http(url, timeout, headers)
    return body.decode("utf-8", "replace") if status == 200 else None


def timed_json(url, timeout=2, headers=None):
    """(status, parsed json or None, latency ms)."""
    t = time.perf_counter()
    status, body = http(url, timeout, headers)
    ms = round((time.perf_counter() - t) * 1000, 1)
    try:
        return status, json.loads(body) if body else None, ms
    except ValueError:
        return status, None, ms


def clean(text):
    """Model output -> single-width printable text that can't break a character grid."""
    out = []
    for ch in str(text).replace("\t", "    "):
        if ch == "\n":
            out.append(ch)
        elif ord(ch) < 32 or unicodedata.combining(ch):
            continue
        elif ord(ch) > 0xFFFF or unicodedata.east_asian_width(ch) in ("W", "F"):
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


def stream_chat(url, model, messages, headers=None, max_tokens=4096, cancelled=lambda: False):
    """Stream an OpenAI-compatible chat completion.

    Yields ('reasoning', text), ('content', text), ('stats', (tokens, tok_per_s)) and finally
    ('error', message) if something went wrong. Works with llama.cpp, Ollama, vLLM, LM Studio.
    """
    body = {"messages": messages, "stream": True, "max_tokens": max_tokens,
            "stream_options": {"include_usage": True}}
    if model:
        body["model"] = model
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    t0, first, n = time.time(), None, 0
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for raw in r:
                if cancelled():
                    yield ("error", "stopped")
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                j = json.loads(payload)
                if j.get("timings"):                       # llama.cpp
                    t = j["timings"]
                    yield ("stats", (t.get("predicted_n"), t.get("predicted_per_second")))
                    n = -1
                elif j.get("usage") and n >= 0:            # OpenAI-style usage (vLLM, Ollama)
                    toks = j["usage"].get("completion_tokens")
                    secs = time.time() - (first or t0)
                    if toks:
                        yield ("stats", (toks, toks / secs if secs > 0 else None))
                        n = -1
                for c in j.get("choices") or []:
                    delta = c.get("delta") or {}
                    think = delta.get("reasoning_content") or delta.get("reasoning")
                    if think:
                        yield ("reasoning", clean(think))
                    if delta.get("content"):
                        first = first or time.time()
                        if n >= 0:
                            n += 1
                        yield ("content", clean(delta["content"]))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {})
            msg = msg.get("message") if isinstance(msg, dict) else msg
        except Exception:
            msg = None
        yield ("error", f"http {e.code}" + (f": {msg}" if msg else ""))
    except Exception as e:  # noqa: BLE001 - surface any failure in the chat window
        yield ("error", str(e)[:120])
