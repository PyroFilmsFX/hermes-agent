"""MCP client-side handlers for server-initiated requests: sampling
(sampling/createMessage, text and tool-use results) and elicitation."""

import asyncio
import functools
import json
import logging
import threading
import time
import uuid
from contextvars import Context
from typing import TYPE_CHECKING, Any, Callable, List, Optional
from tools.mcp_tool_common import _MISSING, _exc_str, _safe_numeric, _sanitize_error, mcp_field, _core
from tools.mcp_tool_schema import _normalize_mcp_input_schema

if TYPE_CHECKING:  # annotations only; mcp_tool imports this module, so a real import is circular
    pass

logger = logging.getLogger("tools.mcp_tool")


def _tool_use_id(block):
    """Tool-use id (marks a tool *result* block) under both SDK spellings — on mcp 2.x a bare
    ``hasattr(b, "toolUseId")`` is False and would silently drop tool results."""
    return mcp_field(block, "tool_use_id", "toolUseId", _MISSING)


def _tool_result_text(block) -> str:
    """Text of a ToolResultContent block ("" when it carries no content)."""
    content = getattr(block, "content", None)
    if content is None:
        return ""
    items = content if isinstance(content, list) else [content]
    return "\n".join(item.text for item in items if hasattr(item, "text"))


def _content_part(block) -> Optional[dict]:
    """One OpenAI content part for a text/image block; None when unsupported."""
    if hasattr(block, "text"):
        return {"type": "text", "text": block.text}
    mime = mcp_field(block, "mime_type", "mimeType", _MISSING)
    if hasattr(block, "data") and mime is not _MISSING:
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{block.data}"}}
    logger.warning("Unsupported sampling content block type: %s (skipped)", type(block).__name__)
    return None


def _tool_call_dict(tu, index: int) -> dict:
    args = tu.input
    return {"id": getattr(tu, "id", f"call_{index}"), "type": "function", "function": {
        "name": tu.name, "arguments": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)}}


def _convert_sampling_message(msg) -> List[dict]:
    """One MCP SamplingMessage -> OpenAI messages: tool results first, then either an assistant
    tool_calls message or plain content."""
    blocks = msg.content_as_list if hasattr(msg, "content_as_list") else (
        msg.content if isinstance(msg.content, list) else [msg.content])
    tool_results = [b for b in blocks if _tool_use_id(b) is not _MISSING]
    others = [b for b in blocks if _tool_use_id(b) is _MISSING]
    tool_uses = [b for b in others if hasattr(b, "name") and hasattr(b, "input")]
    content_blocks = [b for b in others if not (hasattr(b, "name") and hasattr(b, "input"))]
    out = [{"role": "tool", "tool_call_id": _tool_use_id(tr), "content": _tool_result_text(tr)} for tr in tool_results]
    if tool_uses:
        msg_dict: dict = {"role": msg.role, "tool_calls": [_tool_call_dict(tu, i) for i, tu in enumerate(tool_uses)]}
        text_parts = [b.text for b in content_blocks if hasattr(b, "text")]
        if text_parts:
            msg_dict["content"] = "\n".join(text_parts)
        out.append(msg_dict)
    elif len(content_blocks) == 1 and hasattr(content_blocks[0], "text"):
        out.append({"role": msg.role, "content": content_blocks[0].text})
    elif content_blocks:
        parts = [p for p in map(_content_part, content_blocks) if p is not None]
        if parts:
            out.append({"role": msg.role, "content": parts})
    return out


def _parse_tool_call_arguments(server_name: str, args) -> dict:
    """LLM tool_calls arguments -> dict; malformed JSON / non-dicts become ``{"_raw": ...}``, not dropped."""
    if isinstance(args, str):
        try:
            return json.loads(args)
        except (json.JSONDecodeError, ValueError):
            logger.warning("MCP server '%s': malformed tool_calls arguments from LLM (wrapping as raw): %.100s",
                           server_name, args)
            return {"_raw": args}
    return args if isinstance(args, dict) else {"_raw": str(args)}


class SamplingHandler:
    """``sampling_callback`` for one MCP server (per-instance rate-limit, metrics, tool-loop state).
    Runs on the MCP loop; the sync LLM call is offloaded via ``asyncio.to_thread``. Deprecated
    upstream (MCP 2026-07-28, SEP-2577): stays functional for handshake-era servers, but do NOT grow
    new capability here — modern servers use MRTR, handled by the SDK session layer."""

    _STOP_REASON_MAP = {"stop": "endTurn", "length": "maxTokens", "tool_calls": "toolUse"}
    _LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}

    def __init__(self, server_name: str, config: dict):
        self.server_name = server_name
        self.max_rpm = _safe_numeric(config.get("max_rpm", 10), 10, int)
        self.timeout = _safe_numeric(config.get("timeout", 30), 30, float)
        self.max_tokens_cap = _safe_numeric(config.get("max_tokens_cap", 4096), 4096, int)
        self.max_tool_rounds = _safe_numeric(config.get("max_tool_rounds", 5), 5, int, minimum=0)
        self.model_override = config.get("model")
        self.allowed_models = config.get("allowed_models", [])
        self.audit_level = self._LOG_LEVELS.get(str(config.get("log_level", "info")).lower(), logging.INFO)
        self._rate_timestamps: List[float] = []
        self._tool_loop_count = 0
        self.metrics = {"requests": 0, "errors": 0, "tokens_used": 0, "tool_use_count": 0}

    def _check_rate_limit(self) -> bool:
        """Sliding-window (60s) limiter; True if the request is allowed."""
        now = time.time()
        self._rate_timestamps[:] = [t for t in self._rate_timestamps if t > now - 60]
        if len(self._rate_timestamps) >= self.max_rpm:
            return False
        self._rate_timestamps.append(now)
        return True

    def _resolve_model(self, preferences) -> Optional[str]:
        """Config override > server hint > None (use default)."""
        if self.model_override:
            return self.model_override
        hints = getattr(preferences, "hints", None) or []
        return next((hint.name for hint in hints if getattr(hint, "name", None)), None)

    def _convert_messages(self, params) -> List[dict]:
        """MCP SamplingMessages -> OpenAI format (per-block duck-typed dispatch)."""
        return [m for msg in params.messages for m in _convert_sampling_message(msg)]

    @staticmethod
    def _error(message: str, code: int = -1):
        """Return ErrorData (MCP spec) or raise as fallback."""
        if not _core._MCP_SAMPLING_TYPES:
            raise Exception(message)
        return _core.ErrorData(code=code, message=message)

    def _fail(self, message: str):
        """Count an error and return the ErrorData for it."""
        self.metrics["errors"] += 1
        return self._error(message)

    def _log_response(self, response, suffix: str = "", *args) -> None:
        logger.log(self.audit_level, "MCP server '%s' sampling response: model=%s, tokens=%s" + suffix,
                   self.server_name, response.model, getattr(getattr(response, "usage", None), "total_tokens", "?"), *args)

    def _build_tool_use_result(self, choice, response):
        """CreateMessageResultWithTools from a tool_calls response, under ``max_tool_rounds`` (0 disables)."""
        self.metrics["tool_use_count"] += 1
        self._tool_loop_count += 1
        if self.max_tool_rounds == 0 or self._tool_loop_count > self.max_tool_rounds:
            self._tool_loop_count = 0
            return self._error(
                f"Tool loops disabled for server '{self.server_name}' (max_tool_rounds=0)" if self.max_tool_rounds == 0
                else f"Tool loop limit exceeded for server '{self.server_name}' (max {self.max_tool_rounds} rounds)")
        content_blocks = [_core.ToolUseContent(type="tool_use", id=tc.id, name=tc.function.name,
                                               input=_parse_tool_call_arguments(self.server_name, tc.function.arguments))
                          for tc in choice.message.tool_calls]
        self._log_response(response, ", tool_calls=%d", len(content_blocks))
        return _core.CreateMessageResultWithTools(
            role="assistant", content=content_blocks, model=response.model, stopReason="toolUse")

    def _build_text_result(self, choice, response):
        """CreateMessageResult from a normal text response (resets the tool loop)."""
        self._tool_loop_count = 0
        self._log_response(response)
        return _core.CreateMessageResult(
            role="assistant", model=response.model,
            content=_core.TextContent(type="text", text=_sanitize_error(choice.message.content or "")),
            stopReason=self._STOP_REASON_MAP.get(choice.finish_reason, "endTurn"))

    def session_kwargs(self) -> dict:
        """Kwargs to pass to ClientSession for sampling support."""
        return {"sampling_callback": self,
                "sampling_capabilities": _core.SamplingCapability(tools=_core.SamplingToolsCapability())}

    def _admit(self, params):
        """Rate-limit + allowed_models gate. Returns ``(resolved_model, None)`` or ``(None, ErrorData)``."""
        if not self._check_rate_limit():
            logger.warning("MCP server '%s' sampling rate limit exceeded (%d/min)", self.server_name, self.max_rpm)
            return None, self._fail(
                f"Sampling rate limit exceeded for server '{self.server_name}' ({self.max_rpm} requests/minute)")
        resolved_model = self._resolve_model(mcp_field(params, "model_preferences", "modelPreferences")) or ""
        if self.allowed_models and resolved_model and resolved_model not in self.allowed_models:
            logger.warning("MCP server '%s' requested model '%s' not in allowed_models",
                           self.server_name, resolved_model)
            return None, self._fail(f"Model '{resolved_model}' not allowed for server "
                                    f"'{self.server_name}'. Allowed: {', '.join(self.allowed_models)}")
        return resolved_model, None

    def _build_llm_call(self, params, resolved_model: str) -> Callable[[], object]:
        """Sampling params -> zero-arg sync ``call_llm`` thunk (run off-loop); server tools are forwarded."""
        from agent.auxiliary_client import call_llm

        messages = self._convert_messages(params)
        system_prompt = mcp_field(params, "system_prompt", "systemPrompt")
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})
        max_tokens = min(mcp_field(params, "max_tokens", "maxTokens", self.max_tokens_cap), self.max_tokens_cap)
        server_tools = getattr(params, "tools", None)
        tools = [{"type": "function", "function": {
            "name": getattr(t, "name", ""), "description": getattr(t, "description", "") or "",
            "parameters": _normalize_mcp_input_schema(mcp_field(t, "input_schema", "inputSchema"))}}
            for t in server_tools] if server_tools else None
        logger.log(self.audit_level, "MCP server '%s' sampling request: model=%s, max_tokens=%d, messages=%d",
                   self.server_name, resolved_model, max_tokens, len(messages))
        return lambda: call_llm(task="mcp", model=resolved_model or None, messages=messages, max_tokens=max_tokens,
                                temperature=getattr(params, "temperature", None), tools=tools, timeout=self.timeout)

    async def __call__(self, context, params):
        """SDK ``SamplingFnT``: CreateMessageResult, CreateMessageResultWithTools, or ErrorData."""
        resolved_model, err = self._admit(params)
        if err is not None:
            return err
        sync_call = self._build_llm_call(params, resolved_model)  # outside the try: its errors propagate, not _fail
        try:
            response = await asyncio.wait_for(asyncio.to_thread(sync_call), timeout=self.timeout)
        except asyncio.TimeoutError:
            return self._fail(f"Sampling LLM call timed out after {self.timeout}s for server '{self.server_name}'")
        except Exception as exc:
            return self._fail(f"Sampling LLM call failed: {_sanitize_error(_exc_str(exc))}")
        # Empty choices happen on content filtering / provider errors.
        if not getattr(response, "choices", None):
            return self._fail(f"LLM returned empty response (no choices) for server '{self.server_name}'")
        choice = response.choices[0]
        self.metrics["requests"] += 1
        total_tokens = getattr(getattr(response, "usage", None), "total_tokens", 0)
        self.metrics["tokens_used"] += total_tokens if isinstance(total_tokens, int) else 0
        if choice.finish_reason == "tool_calls" and getattr(choice.message, "tool_calls", None):
            return self._build_tool_use_result(choice, response)
        return self._build_text_result(choice, response)


def _format_elicitation_schema_summary(schema: dict, server_name: str) -> str:
    """Flat-object requested_schema -> readable field list so the user knows what they're approving."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return f"Approval requested by MCP server '{server_name}'."
    lines = [f"Fields requested by MCP server '{server_name}':"]
    for field_name, field_spec in props.items():
        spec = field_spec if isinstance(field_spec, dict) else {}
        field_type, field_desc = str(spec.get("type", "") or ""), str(spec.get("description", "") or "")
        lines.append(f"  - {field_name}" + (f" ({field_type})" if field_type else "") + (f": {field_desc}" if field_desc else ""))
    return "\n".join(lines)


class PendingElicitation:
    """In-flight elicitation request awaiting desktop response."""

    def __init__(self, request_id: str, server_name: str, mode: str, message: str,
                 requested_schema: dict | None, url: str | None,
                 loop: asyncio.AbstractEventLoop, future: asyncio.Future):
        self.request_id = request_id
        self.server_name = server_name
        self.mode = mode
        self.message = message
        self.requested_schema = requested_schema or {}
        self.url = url
        self.loop = loop
        self.future = future
        self.created_at = time.time()


_pending_elicitations: dict[str, PendingElicitation] = {}
_pending_elicitations_lock = threading.Lock()


def _has_connected_desktop_clients() -> bool:
    """Fail-closed check: True when at least one desktop/TUI client is attached."""
    try:
        from tui_gateway import server as gw_server
        with gw_server._live_transports_lock:
            return bool(gw_server._live_transports)
    except Exception:
        return False


def _get_elicitation_timeout(fallback: float = 300.0) -> float:
    """Read mcp.elicitation_timeout_seconds from config.yaml (no env var, default 300)."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        val = (cfg.get("mcp") or {}).get("elicitation_timeout_seconds")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return fallback


def _emit_elicitation_request(payload: dict) -> None:
    """Forward elicitation request to connected desktop clients."""
    try:
        from tui_gateway.methods_tools import emit_elicitation_request
        emit_elicitation_request(payload)
    except Exception as exc:
        logger.warning("Failed to emit elicitation request event: %s", exc)


def validate_elicitation_content(content: Any, schema: dict | None) -> tuple[bool, str | None]:
    """Validate content against requestedSchema (flat object of string/number/integer/boolean/enum)."""
    if not isinstance(content, dict):
        return False, "Content must be a JSON object"
    if not schema or not isinstance(schema, dict):
        return True, None

    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    for req_field in required:
        if req_field not in content or content[req_field] is None:
            return False, f"Missing required field: '{req_field}'"

    for key, val in content.items():
        if key in properties:
            spec = properties[key]
            if not isinstance(spec, dict):
                continue
            if "enum" in spec:
                allowed = spec["enum"]
                if val not in allowed:
                    return False, f"Field '{key}' value {val!r} not in enum {allowed}"
            if val is None:
                continue
            expected_type = spec.get("type")
            if expected_type == "string":
                if not isinstance(val, str):
                    return False, f"Field '{key}' expected string, got {type(val).__name__}"
            elif expected_type == "integer":
                if isinstance(val, bool) or not isinstance(val, int):
                    return False, f"Field '{key}' expected integer, got {type(val).__name__}"
            elif expected_type == "number":
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    return False, f"Field '{key}' expected number, got {type(val).__name__}"
            elif expected_type == "boolean":
                if not isinstance(val, bool):
                    return False, f"Field '{key}' expected boolean, got {type(val).__name__}"
    return True, None


def respond_elicitation(request_id: str, action: str, content: Any = None) -> dict:
    """Complete a pending MCP elicitation request with accept, decline, or cancel."""
    with _pending_elicitations_lock:
        pending = _pending_elicitations.get(request_id)
        if pending is None:
            raise KeyError(f"Unknown or expired elicitation request: {request_id}")

    action = (action or "").strip().lower()
    if action not in ("accept", "decline", "cancel"):
        raise ValueError(f"Invalid action: '{action}'. Must be 'accept', 'decline', or 'cancel'.")

    if pending.mode == "url":
        if action not in ("accept", "decline"):
            raise ValueError(f"URL-mode elicitation supports 'accept' or 'decline' only, got '{action}'.")
        result = _core.ElicitResult(action=action)
    else:
        if action == "accept":
            valid, err_msg = validate_elicitation_content(content, pending.requested_schema)
            if not valid:
                raise ValueError(f"Invalid elicitation content: {err_msg}")
            result = _core.ElicitResult(action="accept", content=content or {})
        else:
            result = _core.ElicitResult(action=action)

    with _pending_elicitations_lock:
        _pending_elicitations.pop(request_id, None)

    def _resolve():
        if not pending.future.done():
            pending.future.set_result(result)

    pending.loop.call_soon_threadsafe(_resolve)
    return {"ok": True}


def cancel_all_pending_elicitations(server_name: str | None = None) -> None:
    """Cancel pending elicitation requests during backend shutdown."""
    with _pending_elicitations_lock:
        if server_name is None:
            to_cancel = list(_pending_elicitations.values())
            _pending_elicitations.clear()
        else:
            to_cancel = [p for p in _pending_elicitations.values() if p.server_name == server_name]
            for p in to_cancel:
                _pending_elicitations.pop(p.request_id, None)

    for pending in to_cancel:
        def _cancel(fut=pending.future):
            if not fut.done():
                fut.set_result(_core.ElicitResult(action="cancel"))
        pending.loop.call_soon_threadsafe(_cancel)


class ElicitationHandler:
    """``elicitation_callback`` for one MCP server. Forwards to desktop when connected;
    falls back to Hermes' approval consent system for CLI, TUI and messaging. Fail-closed:
    timeout, shutdown, or errors return decline/cancel."""

    # asyncio-side safety net over the approval's own input() timeout so the MCP loop never blocks
    # indefinitely if the inner timeout is bypassed.
    _OUTER_TIMEOUT_GRACE_SECONDS = 5
    # consent answer -> (ElicitResult action, metric); anything else declines.
    _ANSWER_RESULTS = {"accept": ("accept", "accepted"), "cancel": ("cancel", "errors")}

    def __init__(self, server_name: str, config: dict,
                 call_context: Callable[[], Optional[Context]] = lambda: None):
        self.server_name = server_name
        # 5 min mirrors the gateway approval default so async surfaces (Telegram, Slack) can respond.
        self.timeout = _safe_numeric(config.get("timeout", 300), 300, float)
        # Returns the owning MCPServerTask's contextvars snapshot for the in-flight tool call (None
        # between calls). A thunk, not the task: mcp_tool_server_run imports this module, so
        # MCPServerTask cannot be named here.
        self._call_context = call_context
        self.metrics = {"requests": 0, "accepted": 0, "declined": 0, "errors": 0}

    def session_kwargs(self) -> dict:
        """Kwargs to pass to ClientSession for elicitation support."""
        return {"elicitation_callback": self}

    def _result(self, action: str, metric: str):
        """Count *metric* and return ``ElicitResult(action)`` (accept carries empty content)."""
        self.metrics[metric] += 1
        return _core.ElicitResult(action=action, **({"content": {}} if action == "accept" else {}))

    def _consent_thunk(self, message: str, description: str) -> Callable[[], str]:
        """Sync consent call replaying the agent's contextvars snapshot when the owning task captured one
        (the recv-loop task does NOT inherit them; gateway-platform detection needs them).
        ``Context.run`` runs a context once, so it is copied per elicitation."""
        from tools.approval_prompt import request_elicitation_consent

        consent = functools.partial(request_elicitation_consent, message, description,
                                    timeout_seconds=int(self.timeout), surface=f"mcp-elicitation/{self.server_name}")
        captured = self._call_context()
        return consent if captured is None else (lambda: captured.copy().run(consent))

    async def __call__(self, context, params):
        """SDK elicitation callback (``ElicitationFnT``). Returns ElicitResult or ErrorData."""
        self.metrics["requests"] += 1

        if _has_connected_desktop_clients():
            request_id = str(uuid.uuid4())
            mode = getattr(params, "mode", "form") or "form"
            message = getattr(params, "message", "") or f"MCP server '{self.server_name}' is requesting your input"

            if mode == "url":
                url = getattr(params, "url", None) or ""
                schema = None
                payload = {
                    "request_id": request_id,
                    "server": self.server_name,
                    "message": message,
                    "mode": "url",
                    "url": url,
                }
            else:
                mode = "form"
                url = None
                schema = getattr(params, "requestedSchema", None) or getattr(params, "requested_schema", None) or {}
                if hasattr(schema, "model_dump"):
                    schema = schema.model_dump(by_alias=True, mode="json", exclude_none=True)
                elif not isinstance(schema, dict):
                    schema = {}
                payload = {
                    "request_id": request_id,
                    "server": self.server_name,
                    "message": message,
                    "mode": "form",
                    "requestedSchema": schema,
                }

            logger.info("MCP server '%s' forwarding elicitation request %s (mode=%s): %s",
                        self.server_name, request_id, mode, _sanitize_error(message)[:200])

            loop = asyncio.get_running_loop()
            future = loop.create_future()
            pending = PendingElicitation(
                request_id=request_id,
                server_name=self.server_name,
                mode=mode,
                message=message,
                requested_schema=schema,
                url=url,
                loop=loop,
                future=future,
            )
            with _pending_elicitations_lock:
                _pending_elicitations[request_id] = pending

            try:
                _emit_elicitation_request(payload)
                timeout = _get_elicitation_timeout(self.timeout)

                server = _core._servers.get(self.server_name)
                if server is None:
                    for k, s in _core._servers.items():
                        if getattr(k, "name", str(k)) == self.server_name or str(k) == self.server_name:
                            server = s
                            break
                shutdown_event = getattr(server, "_shutdown_event", None)

                if shutdown_event is not None:
                    shutdown_task = asyncio.create_task(shutdown_event.wait())
                    wait_task = asyncio.ensure_future(future)
                    try:
                        done, _ = await asyncio.wait(
                            {wait_task, shutdown_task},
                            timeout=timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if shutdown_task in done and not wait_task.done():
                            logger.info("MCP server '%s' shutdown mid-elicitation; cancelling", self.server_name)
                            return self._result("cancel", "errors")
                        if wait_task in done:
                            result = wait_task.result()
                            metric = "accepted" if result.action == "accept" else ("declined" if result.action == "decline" else "errors")
                            self.metrics[metric] += 1
                            return result
                        logger.warning("MCP server '%s' elicitation timed out after %gs", self.server_name, timeout)
                        return self._result("decline", "declined")
                    finally:
                        shutdown_task.cancel()
                else:
                    try:
                        result = await asyncio.wait_for(future, timeout=timeout)
                        metric = "accepted" if result.action == "accept" else ("declined" if result.action == "decline" else "errors")
                        self.metrics[metric] += 1
                        return result
                    except asyncio.TimeoutError:
                        logger.warning("MCP server '%s' elicitation timed out after %gs", self.server_name, timeout)
                        return self._result("decline", "declined")
            except Exception as exc:
                logger.error("MCP server '%s' elicitation failed: %s", self.server_name, exc, exc_info=True)
                return self._result("decline", "errors")
            finally:
                with _pending_elicitations_lock:
                    _pending_elicitations.pop(request_id, None)

        if getattr(params, "mode", "form") == "url":  # OAuth/payment: needs a browser + elicitation/complete; unsupported
            logger.info("MCP server '%s' requested URL-mode elicitation; declining "
                        "(URL-mode elicitation not implemented)", self.server_name)
            return self._result("decline", "declined")

        message = getattr(params, "message", "") or f"MCP server '{self.server_name}' is requesting your approval"
        # ``requestedSchema`` on mcp 1.x, ``requested_schema`` on 2.0 (aliases don't apply to attribute
        # access) — read both or the user approves without seeing the fields.
        schema = getattr(params, "requestedSchema", None) or getattr(params, "requested_schema", None) or {}
        logger.info("MCP server '%s' elicitation request: %s", self.server_name, _sanitize_error(message)[:200])
        try:  # lazy import inside avoids import-order coupling with early-bootstrap tools.approval
            invoke_consent = self._consent_thunk(message, _format_elicitation_schema_summary(schema, self.server_name))
        except Exception as exc:  # pragma: no cover -- defensive
            logger.error("MCP server '%s' elicitation: approval system unavailable: %s", self.server_name, exc)
            return self._result("decline", "errors")
        try:  # off-thread: inline, the sync consent flow would freeze the MCP loop and every RPC on it
            answer = await asyncio.wait_for(
                asyncio.to_thread(invoke_consent), timeout=self.timeout + self._OUTER_TIMEOUT_GRACE_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("MCP server '%s' elicitation timed out after %ds", self.server_name, int(self.timeout))
            return self._result("cancel", "errors")
        except Exception as exc:
            logger.error("MCP server '%s' elicitation failed: %s", self.server_name, exc, exc_info=True)
            return self._result("decline", "errors")
        return self._result(*self._ANSWER_RESULTS.get(answer, ("decline", "declined")))


async def call_tool_with_elicitation(server: Any, tool_name: str, args: dict | None = None) -> Any:
    """Drive tool call handling any InputRequiredResult with the client elicitation callback."""
    from mcp_types import InputRequiredResult
    from mcp.client._input_required import run_input_required_driver
    from mcp.client.session import ClientRequestContext

    session = server.session
    async def retry(r=None, s=None):
        return await session.call_tool(tool_name, arguments=args, input_responses=r, request_state=s, allow_input_required=True)

    first = await retry()
    if isinstance(first, InputRequiredResult):
        async def dispatch(key, req):
            ctx = ClientRequestContext(session=session, request_id=key, meta=req.params.meta if req.params else None)
            return await session.dispatch_input_request(ctx, req)
        return await run_input_required_driver(first, dispatch=dispatch, retry=retry)
    return first
