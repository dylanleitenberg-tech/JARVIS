"""The language model behind J.A.R.V.I.S.

Four interchangeable backends, chosen by `ai.backend` in jarvis.json:

  anthropic  Claude, with full tool use over the action registry (default).
  ollama     A model running locally. No key, no account, no network, no
             session anywhere else, and fast enough to answer while you are
             still listening. Needs `ollama pull <ai.model>` once.
  openai     Any other OpenAI-compatible endpoint — LM Studio, vLLM, a hosted
             provider — via `ai.base_url`. Tool use where supported.
  command    An arbitrary local program. The prompt goes in on stdin, the
             reply comes back on stdout. This is the "connect your own AI" hook.
  claude-code  The `command` backend pointed at bridge/claude_code.py, which
             calls the Claude Code CLI. Uses your existing CLI login rather
             than an API key. Slower (a few seconds), so the local intent
             parser matters more here.
  offline    No model at all: the local intent parser handles what it can and
             J.A.R.V.I.S. says so for the rest.

Whatever the backend, the contract is the same: `ask()` streams text to the bus
as it arrives and returns the final spoken line.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

TOOL_ROUNDS = 4  # how many times the model may call tools before we stop
OLLAMA_URL = "http://localhost:11434/v1"
# Small enough to answer a spoken question in about a second on Apple silicon,
# and one of the few at that size that calls tools reliably.
LOCAL_MODEL = "qwen3:8b"


_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_THINK_OPEN = re.compile(r"<(think|thinking|reasoning)>.*$", re.S | re.I)


def _spoken_only(content: Any) -> str:
    """Strip a reasoning model's scratchpad before any of it is said aloud.

    Qwen and the other local thinkers emit <think>...</think> inline, and over
    the OpenAI-compatible endpoint it arrives in `content` like any other text.
    Spoken verbatim it is a paragraph of deliberation in place of an answer.
    An unterminated block is truncation, so everything after it goes too.
    """
    if not isinstance(content, str):
        return ""
    return _THINK_OPEN.sub("", _THINK.sub("", content)).strip()


class Brain:
    def __init__(self, config: dict, bus, dispatcher):
        self.cfg = config["ai"]
        self.bus = bus
        self.dispatcher = dispatcher
        self.backend = self.cfg.get("backend", "anthropic")
        self.history: List[Dict[str, Any]] = []
        self.client = None
        self.status = "offline"
        self.detail = ""
        self._local = False          # an ollama model, holding RAM while loaded
        self._idle_task = None
        self._init_client()

    # -------------------------------------------------------------- setup

    def _init_client(self) -> None:
        if self.backend == "anthropic":
            key = os.environ.get(self.cfg.get("api_key_env") or "ANTHROPIC_API_KEY")
            if not key:
                # Going straight to offline was wrong: it left J.A.R.V.I.S.
                # with no model at all while a signed-in Claude Code CLI sat on
                # the same machine, and the only sign was one line of config
                # nobody reads. Fall through to whatever is actually here.
                self.backend = "claude-code"
                self._init_client()
                if self.status == "ready":
                    self.detail += " (no API key; using the Claude Code login)"
                else:
                    self.backend, self.status = "offline", "no-key"
                    self.detail = (f"{self.cfg.get('api_key_env', 'ANTHROPIC_API_KEY')} "
                                   f"is not set and no local model is running; "
                                   f"local intents only")
                return
            try:
                import anthropic
                self.client = anthropic.AsyncAnthropic(api_key=key)
                self.status, self.detail = "ready", self.cfg["model"]
            except Exception as exc:
                self.backend, self.status, self.detail = "offline", "error", str(exc)
        elif self.backend in ("openai", "ollama"):
            # Ollama speaks the OpenAI protocol, so it is the same client with
            # a different address. It is named separately because it is the one
            # backend that needs no key, no account and no network, and having
            # to know its port to use it made it look unsupported.
            local = self.backend == "ollama"
            if local:
                if not self.cfg.get("base_url"):
                    self.cfg["base_url"] = OLLAMA_URL
                # The single biggest lever on a local model, and it is not
                # optional here: qwen3 deliberates before every answer, which
                # cost 8.4 s to set the volume and timed out outright on
                # "take a screenshot". Switched off it is 1.4 s and picks the
                # same tool. A voice assistant that thinks for eight seconds
                # is not one you would speak to twice. Set ai.reasoning_effort
                # to "low" or "medium" to trade it back for harder questions.
                if not self.cfg.get("reasoning_effort"):
                    self.cfg["reasoning_effort"] = "none"
                # `ai.model` normally holds an API model name. Switching
                # backend alone should not then tell you to `ollama pull
                # claude-sonnet-5`, which is not a thing that exists.
                if re.match(r"^(claude|gpt|o\d)", str(self.cfg.get("model", "")), re.I):
                    self.cfg["model"] = self.cfg.get("local_model") or LOCAL_MODEL
            self.backend = "openai"
            try:
                import httpx
                self.client = httpx.AsyncClient(
                    base_url=self.cfg.get("base_url") or "https://api.openai.com/v1",
                    timeout=60.0,
                    headers={"Authorization": f"Bearer {os.environ.get(self.cfg.get('api_key_env') or 'OPENAI_API_KEY', 'local')}"},
                )
                self.status, self.detail = "ready", f"{self.cfg.get('base_url')} {self.cfg['model']}"
                if local:
                    self._local = True
                    self.detail = self._ollama_detail()
            except Exception as exc:
                self.backend, self.status, self.detail = "offline", "error", str(exc)
        elif self.backend in ("command", "claude-code"):
            if self.backend == "claude-code" and not self.cfg.get("command"):
                # Convenience alias: route through the bundled Claude Code
                # bridge, which uses your existing CLI login, not an API key.
                bridge = pathlib.Path(__file__).resolve().parents[2] / "bridge" / "claude_code.py"
                if not bridge.exists():
                    self.backend, self.status, self.detail = "offline", "no-bridge", str(bridge)
                    return
                # The bridge locating the CLI at call time is too late: it
                # would report "ready" now and fail at the first question.
                if not self._claude_binary(bridge):
                    self.backend, self.status = "offline", "no-cli"
                    self.detail = "the claude CLI is not installed"
                    return
                self.cfg["command"] = [sys.executable, str(bridge)]
            if not self.cfg.get("command"):
                self.backend, self.status = "offline", "no-command"
                self.detail = "ai.command is unset"
            else:
                label = "claude code" if self.backend == "claude-code" \
                    else pathlib.Path(self.cfg["command"][-1]).name
                self.backend, self.status, self.detail = "command", "ready", label
        else:
            self.status, self.detail = "offline", "intents only"

    @staticmethod
    def _claude_binary(bridge: pathlib.Path) -> str:
        """Ask the bridge where the CLI is, without paying for a subprocess."""
        import importlib.util

        try:
            spec = importlib.util.spec_from_file_location("_jarvis_bridge", bridge)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.find_binary()
        except Exception:
            return ""

    def _ollama_detail(self) -> str:
        """Say which of the three ways this can be wrong it actually is.

        A local model fails as a silent timeout at the first question, hours
        after the config said "ready". Checking at startup costs one request to
        localhost and turns that into a line on the HUD.
        """
        import httpx

        want = str(self.cfg.get("model", ""))
        try:
            tags = httpx.get(OLLAMA_URL.replace("/v1", "/api/tags"), timeout=2.0).json()
        except Exception:
            self.status = "error"
            return "ollama is not running — start it with: ollama serve"
        names = [str(m.get("name", "")) for m in tags.get("models") or []]
        if not names:
            self.status = "error"
            return f"ollama has no models — pull one with: ollama pull {want or 'qwen3:8b'}"
        # Tags are "qwen3:8b"; a config that says "qwen3" should still match.
        if want and not any(n == want or n.split(":")[0] == want.split(":")[0]
                            for n in names):
            self.status = "error"
            return f"ollama has no {want}; it has {', '.join(names[:4])}"
        return f"ollama {want or names[0]}"

    def info(self) -> Dict[str, str]:
        return {"backend": self.backend, "status": self.status,
                "detail": self.detail, "model": self.cfg.get("model", "")}

    # --------------------------------------------------------------- ask

    async def ask(self, text: str) -> str:
        """Answer an utterance, running tools as needed. Returns the spoken line."""
        await self.bus.publish("thinking", on=True)
        try:
            if self.backend == "anthropic":
                reply = await self._ask_anthropic(text)
            elif self.backend == "openai":
                reply = await self._ask_openai(text)
            elif self.backend == "command":
                reply = await self._ask_command(text)
            else:
                reply = ("I did not follow that, Sir, and I have no model connected. "
                         "Try a direct command.")
        except Exception as exc:
            reply = f"My connection to the model failed. {exc.__class__.__name__}."
            await self.bus.publish("log", level="error",
                                   text=f"brain: {exc.__class__.__name__}: {exc}")
        finally:
            await self.bus.publish("thinking", on=False)
            self._touch()
        return reply

    # ------------------------------------------------------- local model RAM

    def _touch(self) -> None:
        """Restart the idle countdown after every question."""
        if not self._local or not self.cfg.get("unload_after"):
            return
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        try:
            self._idle_task = asyncio.get_running_loop().create_task(self._unload_later())
        except RuntimeError:          # no loop: nothing to schedule against
            self._idle_task = None

    async def _unload_later(self) -> None:
        """Hand the model's memory back once the conversation has stopped.

        An 8B model holds about 6 GB resident, and ollama keeps it loaded for
        five minutes after each question with no way to say otherwise over the
        OpenAI-compatible endpoint — `keep_alive` is accepted there and
        ignored. On a 16 GB machine also running the camera, Chrome and the
        HUD that is most of the headroom, held for a conversation that ended.

        Unloading is only worth it because reloading is cheap: 3.7 s cold
        against 1.1 s warm on Apple silicon. So the model stays resident while
        you are actually talking and goes away when you stop.
        """
        try:
            await asyncio.sleep(float(self.cfg["unload_after"]))
            base = (self.cfg.get("base_url") or OLLAMA_URL).rsplit("/v1", 1)[0]
            import httpx
            async with httpx.AsyncClient(timeout=20.0) as client:
                await client.post(f"{base}/api/generate",
                                  json={"model": self.cfg["model"], "keep_alive": 0})
            await self.bus.publish("log", level="info",
                                   text=f"brain: unloaded {self.cfg['model']} after "
                                        f"{self.cfg['unload_after']:.0f}s idle")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass          # a model that will not unload is not worth a failure

    def _trim(self) -> None:
        limit = int(self.cfg.get("history_turns", 12)) * 2
        if len(self.history) > limit:
            # Never start the window on a tool_result, which must follow its call.
            cut = len(self.history) - limit
            while cut < len(self.history):
                first = self.history[cut]
                content = first.get("content")
                if first["role"] == "user" and isinstance(content, list) and \
                        any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    cut += 1
                    continue
                break
            self.history = self.history[cut:]

    # -------------------------------------------------------- anthropic

    async def _ask_anthropic(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})
        self._trim()
        tools = self.dispatcher.tool_specs() if self.cfg.get("allow_tools", True) else []
        spoken = ""

        for _round in range(TOOL_ROUNDS):
            kwargs: Dict[str, Any] = {
                "model": self.cfg["model"],
                "max_tokens": int(self.cfg["max_tokens"]),
                "system": self.cfg["system"],
                "messages": self.history,
            }
            if tools:
                kwargs["tools"] = tools

            chunk = ""
            async with self.client.messages.stream(**kwargs) as stream:
                async for delta in stream.text_stream:
                    chunk += delta
                    await self.bus.publish("say_partial", text=chunk)
                message = await stream.get_final_message()

            self.history.append({"role": "assistant", "content": message.content})
            said = "".join(b.text for b in message.content if b.type == "text").strip()
            if said:
                spoken = f"{spoken} {said}".strip()

            calls = [b for b in message.content if b.type == "tool_use"]
            if not calls:
                break

            results = []
            for call in calls:
                outcome = await self.dispatcher.run(call.name, dict(call.input or {}),
                                                    source="model")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "is_error": not outcome.get("ok", False),
                    "content": json.dumps(outcome.get("result") if outcome.get("ok")
                                          else outcome.get("error"), default=str)[:4000],
                })
            self.history.append({"role": "user", "content": results})

        return spoken or "Done."

    # ----------------------------------------------------------- openai

    async def _ask_openai(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})
        self._trim()
        tools = [{"type": "function",
                  "function": {"name": spec["name"], "description": spec["description"],
                               "parameters": spec["input_schema"]}}
                 for spec in self.dispatcher.tool_specs()] \
            if self.cfg.get("allow_tools", True) else []

        spoken = ""
        for _round in range(TOOL_ROUNDS):
            body: Dict[str, Any] = {
                "model": self.cfg["model"],
                "max_tokens": int(self.cfg["max_tokens"]),
                "messages": [{"role": "system", "content": self.cfg["system"]}] + self.history,
            }
            if tools:
                body["tools"] = tools
            if self.cfg.get("reasoning_effort"):
                body["reasoning_effort"] = self.cfg["reasoning_effort"]
            response = await self.client.post("/chat/completions", json=body)
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            self.history.append(message)

            said = _spoken_only(message.get("content"))
            if said:
                spoken = f"{spoken} {said}".strip()
                await self.bus.publish("say_partial", text=spoken)

            calls = message.get("tool_calls") or []
            if not calls:
                break
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                outcome = await self.dispatcher.run(fn["name"], args, source="model")
                self.history.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "content": json.dumps(outcome.get("result") if outcome.get("ok")
                                          else outcome.get("error"), default=str)[:4000],
                })
        return spoken or "Done."

    # ---------------------------------------------------------- command

    async def _ask_command(self, text: str) -> str:
        """Pipe the utterance to a local program and speak whatever it prints.

        The program cannot make real tool calls, so it returns any actions it
        wants as data and J.A.R.V.I.S. runs them through the usual dispatcher —
        which means the confirmation gate still applies to whatever it asks for.
        """
        argv = list(self.cfg["command"])
        payload = json.dumps({
            "text": text,
            "system": self.cfg["system"],
            "tools": self.dispatcher.tool_specs() if self.cfg.get("allow_tools", True) else [],
        })

        def call() -> dict:
            proc = subprocess.run(argv, input=payload, capture_output=True,
                                  text=True, timeout=90)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip()[:300] or "command failed")
            out = proc.stdout.strip()
            try:  # accept bare text, {"text": ...} or {"text":..., "actions":[...]}
                parsed = json.loads(out)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
            return {"text": out}

        result = await asyncio.get_running_loop().run_in_executor(None, call)
        reply = str(result.get("text") or result.get("reply") or "").strip()
        if reply:
            await self.bus.publish("say_partial", text=reply)

        for call_spec in (result.get("actions") or [])[:TOOL_ROUNDS]:
            if not isinstance(call_spec, dict) or not call_spec.get("name"):
                continue
            await self.dispatcher.run(str(call_spec["name"]),
                                      dict(call_spec.get("args") or {}), source="model")

        return reply or "Done."

    def reset(self) -> None:
        self.history.clear()
