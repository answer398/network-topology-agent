"""OpenAI-compatible multimodal calls with local structured validation."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, TypeVar
from urllib.parse import urlsplit

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)
from PIL import Image
from pydantic import BaseModel, SecretStr, ValidationError

from .config import ModelConfig
from .models import ConfigurationError, InputError, ModelInvocationError


_PROMPT_FILES = frozenset({"system.md", "extraction.md", "repair.md"})
_HTTP_RETRY_DELAYS = (1.0, 2.0)
_FENCE_PATTERN = re.compile(
    r"```(?P<label>[^`\r\n]*)\r?\n(?P<body>.*?)```",
    flags=re.DOTALL,
)
_DATA_URL_PATTERN = re.compile(
    r"data:image/[a-zA-Z0-9.+-]+;base64,[a-zA-Z0-9+/=]+",
    flags=re.IGNORECASE,
)
_AUTH_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_SECRET_PATTERN = re.compile(r"(?i)\b(?:sk|key)-[a-zA-Z0-9_-]{12,}\b")


class SkillName(StrEnum):
    TOPOLOGY_RECOGNITION = "topology_recognition"
    NETWORK_REASONING = "network_reasoning"
    PLATFORM_MAPPING = "platform_mapping"


_SKILL_FILES = {skill: f"{skill.value}.md" for skill in SkillName}


@dataclass(frozen=True, slots=True)
class ModelImage:
    """A named, normalized Pillow image supplied by M2 or its caller."""

    view_id: str
    image: Image.Image

    def __post_init__(self) -> None:
        if not isinstance(self.view_id, str) or not self.view_id.strip():
            raise InputError("model image viewId must be a non-empty string")
        if self.view_id != self.view_id.strip() or any(
            ord(character) < 32 for character in self.view_id
        ):
            raise InputError("model image viewId contains invalid whitespace")
        if not isinstance(self.image, Image.Image) or self.image.mode != "RGB":
            raise InputError(
                f"model image {self.view_id!r} must be an RGB Pillow image"
            )
        width, height = self.image.size
        if width <= 0 or height <= 0:
            raise InputError(f"model image {self.view_id!r} has an invalid size")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Token totals with HTTP attempts separated from logical model calls."""

    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    logical_call_count: int = 0


@dataclass(frozen=True, slots=True)
class ModelHttpAttempt:
    """Secret-free timing and size measurements for one HTTP request."""

    attempt_sequence: int
    request_attempt: int
    logical_call_index: int
    stage: str | None
    model: str
    max_tokens: int
    request_started_at: str
    first_byte_at: str | None
    request_ended_at: str
    duration_ms: float
    ttfb_ms: float | None
    request_body_bytes: int
    image_bytes: int
    prompt_bytes: int
    schema_bytes: int
    request_body_sha256: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    finish_reason: str | None = None
    http_status: int | None = None
    exception_type: str | None = None
    retry_cause: str | None = None
    next_max_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "attemptSequence": self.attempt_sequence,
            "attempt": self.request_attempt,
            "logicalCallIndex": self.logical_call_index,
            "stage": self.stage,
            "model": self.model,
            "maxTokens": self.max_tokens,
            "requestStartedAt": self.request_started_at,
            "firstByteAt": self.first_byte_at,
            "requestEndedAt": self.request_ended_at,
            "durationMs": self.duration_ms,
            "ttfbMs": self.ttfb_ms,
            "requestBodyBytes": self.request_body_bytes,
            "imageBytes": self.image_bytes,
            "promptBytes": self.prompt_bytes,
            "schemaBytes": self.schema_bytes,
            "requestBodySha256": self.request_body_sha256,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
            "cachedTokens": self.cached_tokens,
            "finishReason": self.finish_reason,
            "httpStatus": self.http_status,
            "exceptionType": self.exception_type,
            "retryCause": self.retry_cause,
            "nextMaxTokens": self.next_max_tokens,
        }


@dataclass(frozen=True, slots=True)
class _RequestSpec:
    stage: str | None
    model_name: str
    max_tokens: int
    degraded_max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class _RequestMetadata:
    prompt_bytes: int
    schema_bytes: int
    image_bytes: int


@dataclass(slots=True)
class _ActiveHttpAttempt:
    attempt_sequence: int
    request_attempt: int
    logical_call_index: int
    request_spec: _RequestSpec
    max_tokens: int
    metadata: _RequestMetadata
    request_started_at: str
    request_started_monotonic: float
    request_body_bytes: int
    request_body_sha256: str
    first_byte_at: str | None = None
    first_byte_monotonic: float | None = None
    http_status: int | None = None


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ModelCallResult(Generic[T]):
    value: T
    raw_text: str
    model: str
    request_id: str | None
    attempts: int
    usage: ModelUsage
    loaded_skill: SkillName
    repaired: bool
    http_attempts: tuple[ModelHttpAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class RawModelResult:
    raw_text: str
    model: str
    request_id: str | None
    attempts: int
    usage: ModelUsage
    loaded_skill: SkillName | None
    http_attempts: tuple[ModelHttpAttempt, ...] = ()


def load_prompt(
    filename: str, prompt_root: str | Path = Path("prompts")
) -> str:
    """Load one of the three fixed prompt files as UTF-8."""

    if filename not in _PROMPT_FILES:
        raise ConfigurationError(f"unsupported prompt file: {filename!r}")
    return _read_resource(Path(prompt_root), "prompts", filename)


def load_skill(
    skill: SkillName, skill_root: str | Path = Path("skills")
) -> str:
    """Load exactly one whitelisted skill file as UTF-8."""

    if not isinstance(skill, SkillName):
        raise ConfigurationError("skill must be a SkillName value")
    return _read_resource(Path(skill_root), "skills", _SKILL_FILES[skill])


def extract_json_object(text: str) -> str:
    """Return the first valid JSON object text from supported model output forms."""

    if not isinstance(text, str) or not text.strip():
        raise ModelInvocationError("model response did not contain text")
    stripped = text.strip()
    if _is_json_object(stripped):
        return stripped

    for match in _FENCE_PATTERN.finditer(text):
        if match.group("label").strip().lower() not in {"", "json"}:
            continue
        candidate = match.group("body").strip()
        if _is_json_object(candidate):
            return candidate

    for candidate in _balanced_object_candidates(text):
        if _is_json_object(candidate):
            return candidate

    excerpt = _safe_excerpt(text)
    raise ModelInvocationError(
        f"model response did not contain a valid JSON object; excerpt={excerpt!r}"
    )


class OpenAICompatibleModelClient:
    """One synchronous OpenAI-compatible client with an instance call budget."""

    def __init__(
        self,
        *,
        model_config: ModelConfig,
        api_key: SecretStr,
        max_model_calls: int,
        prompt_root: str | Path = Path("prompts"),
        skill_root: str | Path = Path("skills"),
    ) -> None:
        if not isinstance(model_config, ModelConfig):
            raise ConfigurationError("modelConfig must be a ModelConfig")
        if not isinstance(api_key, SecretStr):
            raise ConfigurationError("apiKey must be a SecretStr")
        if (
            isinstance(max_model_calls, bool)
            or not isinstance(max_model_calls, int)
            or max_model_calls <= 0
        ):
            raise ConfigurationError("maxModelCalls must be a positive integer")

        self._model_name = model_config.model_name
        self._base_url = model_config.base_url
        self._enable_thinking = model_config.enable_thinking
        self._temperature = model_config.temperature
        self._max_tokens = model_config.max_tokens
        self._timeout_seconds = model_config.timeout_seconds
        self._text_only_model_name = (
            model_config.text_only_model_name or model_config.model_name
        )
        self._text_max_tokens = model_config.text_stage.max_tokens
        self._text_degraded_max_tokens = model_config.text_stage.degraded_max_tokens
        self._max_model_calls = max_model_calls
        self._prompt_root = Path(prompt_root)
        self._skill_root = Path(skill_root)
        self._prompt_cache: dict[str, str] = {}
        self._skill_cache: dict[SkillName, str] = {}
        self._usage = ModelUsage()
        self._http_attempts: list[ModelHttpAttempt] = []
        self._attempt_sequence = 0
        self._active_http_attempt: _ActiveHttpAttempt | None = None

        secret_value = api_key.get_secret_value()
        if not secret_value.strip():
            raise ConfigurationError("apiKey must not be empty")
        try:
            self._http_client = httpx.Client(
                timeout=self._timeout_seconds,
                event_hooks={"response": [self._on_http_response]},
            )
            self._client = OpenAI(
                api_key=secret_value,
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                max_retries=0,
                http_client=self._http_client,
            )
        except Exception as exc:
            if hasattr(self, "_http_client"):
                self._http_client.close()
            raise ConfigurationError(
                f"cannot create OpenAI-compatible client ({type(exc).__name__})"
            ) from None
        finally:
            del secret_value

    def __repr__(self) -> str:
        host = urlsplit(self._base_url).hostname or "<invalid>"
        return (
            f"{type(self).__name__}(model={self._model_name!r}, host={host!r}, "
            f"logical_calls={self._usage.logical_call_count}, "
            f"requests={self._usage.request_count}, budget={self._max_model_calls})"
        )

    @property
    def usage(self) -> ModelUsage:
        """Return the immutable cumulative usage snapshot for this client."""

        return self._usage

    @property
    def http_attempts(self) -> tuple[ModelHttpAttempt, ...]:
        """Return immutable, secret-free records for every actual HTTP attempt."""

        return tuple(self._http_attempts)

    @property
    def remaining_model_calls(self) -> int:
        """Return the number of unused logical model calls."""

        return self._max_model_calls - self._usage.logical_call_count

    def ensure_model_call_budget(self, required: int) -> None:
        """Fail before a workflow starts unless its logical-call budget remains."""

        if isinstance(required, bool) or not isinstance(required, int) or required <= 0:
            raise InputError("required model calls must be a positive integer")
        if required > self.remaining_model_calls:
            raise self._budget_error(required)

    def call_structured(
        self,
        *,
        task_text: str,
        images: Sequence[ModelImage],
        response_model: type[T],
        skill: SkillName,
        allow_repair: bool = True,
        allow_empty_images: bool = False,
        request_stage: str | None = None,
        response_validation_context: Mapping[str, object] | None = None,
    ) -> ModelCallResult[T]:
        """Call JSON mode and validate the final object with a Pydantic model.

        Empty images are accepted only when the caller explicitly enables the
        topology-recognition text-only fusion path.
        """

        task = _validate_task_text(task_text)
        checked_skill = _validate_skill(skill)
        if not isinstance(allow_repair, bool):
            raise InputError("allowRepair must be a boolean")
        if not isinstance(allow_empty_images, bool):
            raise InputError("allowEmptyImages must be a boolean")
        stage = _validate_request_stage(request_stage)
        if response_validation_context is not None and not isinstance(
            response_validation_context, Mapping
        ):
            raise InputError("responseValidationContext must be an object")
        validation_context = (
            None
            if response_validation_context is None
            else dict(response_validation_context)
        )
        checked_images = _validate_images(
            images, checked_skill, allow_empty_images=allow_empty_images
        )
        if not isinstance(response_model, type) or not issubclass(
            response_model, BaseModel
        ):
            raise InputError("responseModel must be a Pydantic model class")
        try:
            schema = response_model.model_json_schema(by_alias=True)
            schema_text = json.dumps(schema, ensure_ascii=False, indent=2)
        except Exception as exc:
            raise InputError(
                f"cannot generate responseModel JSON Schema ({type(exc).__name__})"
            ) from None

        before = self.usage
        attempt_start = len(self._http_attempts)
        request_spec = self._request_spec(stage, has_images=bool(checked_images))
        system_prompt = self._prompt("system.md")
        skill_prompt = self._skill(checked_skill)
        extraction_prompt = self._prompt("extraction.md")
        user_text = _structured_user_text(
            task, checked_images, skill_prompt, extraction_prompt, schema_text
        )
        messages = _messages(system_prompt, user_text, checked_images)
        request_metadata = _request_metadata(
            system_prompt, user_text, schema_text, messages
        )
        self._consume_logical_call_budget()
        completion, raw_text = self._request_text(
            messages,
            json_mode=True,
            request_spec=request_spec,
            metadata=request_metadata,
        )
        final_completion = completion
        final_request_spec = request_spec
        repaired = False

        try:
            value = _validate_structured_value(
                raw_text, response_model, validation_context
            )
        except (ModelInvocationError, ValidationError) as exc:
            if not allow_repair:
                _raise_output_error("model output", exc)
            repair_prompt = self._prompt("repair.md")
            repair_text = _repair_user_text(
                skill_prompt, repair_prompt, raw_text, _output_error(exc), schema_text
            )
            repair_messages = _messages(system_prompt, repair_text, ())
            repair_request_spec = self._request_spec(stage, has_images=False)
            final_completion, raw_text = self._request_text(
                repair_messages,
                json_mode=True,
                request_spec=repair_request_spec,
                metadata=_request_metadata(
                    system_prompt, repair_text, schema_text, repair_messages
                ),
            )
            final_request_spec = repair_request_spec
            repaired = True
            try:
                value = _validate_structured_value(
                    raw_text, response_model, validation_context
                )
            except (ModelInvocationError, ValidationError) as repair_exc:
                _raise_output_error("model repair output", repair_exc)

        usage = _usage_delta(self.usage, before)
        return ModelCallResult(
            value=value,
            raw_text=raw_text,
            model=_completion_model(final_completion, final_request_spec.model_name),
            request_id=_completion_request_id(final_completion),
            attempts=usage.request_count,
            usage=usage,
            loaded_skill=checked_skill,
            repaired=repaired,
            http_attempts=tuple(self._http_attempts[attempt_start:]),
        )

    def call_text(
        self,
        *,
        task_text: str,
        images: Sequence[ModelImage] = (),
        skill: SkillName | None = None,
    ) -> RawModelResult:
        """Make a small non-streaming text call without local JSON validation."""

        task = _validate_task_text(task_text)
        checked_skill = None if skill is None else _validate_skill(skill)
        checked_images = _validate_images(images, checked_skill)
        before = self.usage
        attempt_start = len(self._http_attempts)
        request_spec = self._request_spec(None, has_images=bool(checked_images))
        system_prompt = (
            self._prompt("system.md")
            + "\n\n## Raw text mode exception\n"
            "This call has no target JSON Schema. Follow the current task's explicit "
            "plain-text or Markdown output format. All fact, security, prompt-injection, "
            "and platform-boundary rules above remain mandatory."
        )
        parts = []
        if checked_skill is not None:
            parts.extend(("## Current skill", self._skill(checked_skill)))
        parts.extend(("## Current task", task, _view_description(checked_images)))
        user_text = "\n\n".join(part for part in parts if part)
        messages = _messages(system_prompt, user_text, checked_images)
        self._consume_logical_call_budget()
        completion, raw_text = self._request_text(
            messages,
            json_mode=False,
            request_spec=request_spec,
            metadata=_request_metadata(system_prompt, user_text, None, messages),
        )
        usage = _usage_delta(self.usage, before)
        return RawModelResult(
            raw_text=raw_text,
            model=_completion_model(completion, request_spec.model_name),
            request_id=_completion_request_id(completion),
            attempts=usage.request_count,
            usage=usage,
            loaded_skill=checked_skill,
            http_attempts=tuple(self._http_attempts[attempt_start:]),
        )

    def _prompt(self, filename: str) -> str:
        if filename not in self._prompt_cache:
            self._prompt_cache[filename] = load_prompt(filename, self._prompt_root)
        return self._prompt_cache[filename]

    def _skill(self, skill: SkillName) -> str:
        if skill not in self._skill_cache:
            self._skill_cache[skill] = load_skill(skill, self._skill_root)
        return self._skill_cache[skill]

    def _request_spec(self, stage: str | None, *, has_images: bool) -> _RequestSpec:
        if not has_images:
            if stage == "text":
                return _RequestSpec(
                    stage=stage,
                    model_name=self._text_only_model_name,
                    max_tokens=self._text_max_tokens,
                    degraded_max_tokens=self._text_degraded_max_tokens,
                )
            return _RequestSpec(
                stage=stage,
                model_name=self._text_only_model_name,
                max_tokens=self._max_tokens,
            )
        if stage == "text":
            return _RequestSpec(
                stage=stage,
                model_name=self._model_name,
                max_tokens=self._text_max_tokens,
                degraded_max_tokens=self._text_degraded_max_tokens,
            )
        return _RequestSpec(
            stage=stage,
            model_name=self._model_name,
            max_tokens=self._max_tokens,
        )

    def _on_http_response(self, response: httpx.Response) -> None:
        active = self._active_http_attempt
        if active is None:
            return
        active.http_status = response.status_code
        if active.first_byte_monotonic is None:
            active.first_byte_at = _utc_timestamp()
            active.first_byte_monotonic = time.perf_counter()

    def _request(
        self,
        messages: list[dict[str, object]],
        *,
        json_mode: bool,
        request_spec: _RequestSpec,
        metadata: _RequestMetadata,
    ) -> Any:
        retry_index = 0
        request_attempt = 0
        timeout_degraded = False
        max_tokens = request_spec.max_tokens
        while True:
            request_attempt += 1
            parameters: dict[str, object] = {
                "model": request_spec.model_name,
                "messages": messages,
                "temperature": self._temperature,
                "max_tokens": max_tokens,
                "extra_body": {"enable_thinking": self._enable_thinking},
            }
            if json_mode:
                parameters["response_format"] = {"type": "json_object"}

            active = self._start_http_attempt(
                request_attempt,
                request_spec,
                max_tokens,
                metadata,
                parameters,
            )
            self._record_http_request()
            self._active_http_attempt = active

            try:
                completion = self._client.chat.completions.create(**parameters)
            except APIStatusError as exc:
                status = exc.status_code
                retry_cause = _retryable_status_cause(status)
                can_retry = retry_cause is not None and retry_index < len(
                    _HTTP_RETRY_DELAYS
                )
                self._finish_http_attempt(
                    active,
                    exception=exc,
                    http_status=status,
                    retry_cause=retry_cause if can_retry else None,
                )
                if can_retry:
                    time.sleep(_HTTP_RETRY_DELAYS[retry_index])
                    retry_index += 1
                    continue
                raise ModelInvocationError(
                    f"model request failed with HTTP {status} ({type(exc).__name__})"
                ) from None
            except APITimeoutError as exc:
                next_max_tokens = self._timeout_degraded_max_tokens(
                    active,
                    max_tokens,
                    request_spec,
                    timeout_degraded,
                )
                self._finish_http_attempt(
                    active,
                    exception=exc,
                    retry_cause=(
                        "read_timeout_no_first_byte_degraded"
                        if next_max_tokens is not None
                        else None
                    ),
                    next_max_tokens=next_max_tokens,
                )
                if next_max_tokens is not None:
                    max_tokens = next_max_tokens
                    timeout_degraded = True
                    continue
                raise ModelInvocationError(
                    f"model request timed out after {self._timeout_seconds:g} seconds "
                    f"({type(exc).__name__})"
                ) from None
            except APIConnectionError as exc:
                if _is_read_timeout(exc):
                    next_max_tokens = self._timeout_degraded_max_tokens(
                        active,
                        max_tokens,
                        request_spec,
                        timeout_degraded,
                    )
                    self._finish_http_attempt(
                        active,
                        exception=exc,
                        retry_cause=(
                            "read_timeout_no_first_byte_degraded"
                            if next_max_tokens is not None
                            else None
                        ),
                        next_max_tokens=next_max_tokens,
                    )
                    if next_max_tokens is not None:
                        max_tokens = next_max_tokens
                        timeout_degraded = True
                        continue
                    raise ModelInvocationError(
                        f"model request timed out after {self._timeout_seconds:g} seconds "
                        f"({type(exc).__name__})"
                    ) from None
                can_retry = retry_index < len(_HTTP_RETRY_DELAYS)
                self._finish_http_attempt(
                    active,
                    exception=exc,
                    retry_cause="connection_error" if can_retry else None,
                )
                if can_retry:
                    time.sleep(_HTTP_RETRY_DELAYS[retry_index])
                    retry_index += 1
                    continue
                raise ModelInvocationError(
                    f"model network request failed ({type(exc).__name__})"
                ) from None
            except APIError as exc:
                self._finish_http_attempt(active, exception=exc)
                raise ModelInvocationError(
                    f"model SDK request failed ({type(exc).__name__})"
                ) from None
            except Exception as exc:
                self._finish_http_attempt(active, exception=exc)
                raise ModelInvocationError(
                    f"unexpected model client failure ({type(exc).__name__})"
                ) from None

            self._record_response_usage(completion)
            self._finish_http_attempt(active, completion=completion)
            return completion

    def _timeout_degraded_max_tokens(
        self,
        active: _ActiveHttpAttempt,
        max_tokens: int,
        request_spec: _RequestSpec,
        timeout_degraded: bool,
    ) -> int | None:
        if (
            timeout_degraded
            or active.first_byte_monotonic is not None
            or request_spec.degraded_max_tokens is None
            or request_spec.degraded_max_tokens >= max_tokens
        ):
            return None
        return request_spec.degraded_max_tokens

    def _start_http_attempt(
        self,
        request_attempt: int,
        request_spec: _RequestSpec,
        max_tokens: int,
        metadata: _RequestMetadata,
        parameters: Mapping[str, object],
    ) -> _ActiveHttpAttempt:
        request_body = _request_body_bytes(parameters)
        self._attempt_sequence += 1
        return _ActiveHttpAttempt(
            attempt_sequence=self._attempt_sequence,
            request_attempt=request_attempt,
            logical_call_index=self._usage.logical_call_count,
            request_spec=request_spec,
            max_tokens=max_tokens,
            metadata=metadata,
            request_started_at=_utc_timestamp(),
            request_started_monotonic=time.perf_counter(),
            request_body_bytes=len(request_body),
            request_body_sha256=hashlib.sha256(request_body).hexdigest(),
        )

    def _finish_http_attempt(
        self,
        active: _ActiveHttpAttempt,
        *,
        completion: Any | None = None,
        exception: Exception | None = None,
        http_status: int | None = None,
        retry_cause: str | None = None,
        next_max_tokens: int | None = None,
    ) -> None:
        ended_at = _utc_timestamp()
        ended_monotonic = time.perf_counter()
        if active.first_byte_monotonic is None and completion is not None:
            active.first_byte_at = ended_at
            active.first_byte_monotonic = ended_monotonic
        usage = _completion_usage(completion)
        duration_ms = max(0.0, (ended_monotonic - active.request_started_monotonic) * 1000)
        ttfb_ms = (
            None
            if active.first_byte_monotonic is None
            else max(
                0.0,
                (active.first_byte_monotonic - active.request_started_monotonic)
                * 1000,
            )
        )
        self._http_attempts.append(
            ModelHttpAttempt(
                attempt_sequence=active.attempt_sequence,
                request_attempt=active.request_attempt,
                logical_call_index=active.logical_call_index,
                stage=active.request_spec.stage,
                model=active.request_spec.model_name,
                max_tokens=active.max_tokens,
                request_started_at=active.request_started_at,
                first_byte_at=active.first_byte_at,
                request_ended_at=ended_at,
                duration_ms=round(duration_ms, 3),
                ttfb_ms=None if ttfb_ms is None else round(ttfb_ms, 3),
                request_body_bytes=active.request_body_bytes,
                image_bytes=active.metadata.image_bytes,
                prompt_bytes=active.metadata.prompt_bytes,
                schema_bytes=active.metadata.schema_bytes,
                request_body_sha256=active.request_body_sha256,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                cached_tokens=usage.cached_tokens,
                finish_reason=_completion_finish_reason(completion),
                http_status=http_status if http_status is not None else active.http_status,
                exception_type=None if exception is None else type(exception).__name__,
                retry_cause=retry_cause,
                next_max_tokens=next_max_tokens,
            )
        )
        if self._active_http_attempt is active:
            self._active_http_attempt = None

    def _request_text(
        self,
        messages: list[dict[str, object]],
        *,
        json_mode: bool,
        request_spec: _RequestSpec,
        metadata: _RequestMetadata,
    ) -> tuple[Any, str]:
        completion = self._request(
            messages,
            json_mode=json_mode,
            request_spec=request_spec,
            metadata=metadata,
        )
        return completion, _completion_text(completion)

    def _consume_logical_call_budget(self) -> None:
        self.ensure_model_call_budget(1)
        self._usage = ModelUsage(
            request_count=self._usage.request_count,
            prompt_tokens=self._usage.prompt_tokens,
            completion_tokens=self._usage.completion_tokens,
            total_tokens=self._usage.total_tokens,
            cached_tokens=self._usage.cached_tokens,
            logical_call_count=self._usage.logical_call_count + 1,
        )

    def _record_http_request(self) -> None:
        self._usage = ModelUsage(
            request_count=self._usage.request_count + 1,
            prompt_tokens=self._usage.prompt_tokens,
            completion_tokens=self._usage.completion_tokens,
            total_tokens=self._usage.total_tokens,
            cached_tokens=self._usage.cached_tokens,
            logical_call_count=self._usage.logical_call_count,
        )

    def _budget_error(self, required: int = 1) -> ModelInvocationError:
        return ModelInvocationError(
            f"model call budget exhausted: {self._usage.logical_call_count}/"
            f"{self._max_model_calls} logical calls used; {required} required"
        )

    def _record_response_usage(self, completion: Any) -> None:
        usage = _completion_usage(completion)
        self._usage = ModelUsage(
            request_count=self._usage.request_count,
            prompt_tokens=self._usage.prompt_tokens + usage.prompt_tokens,
            completion_tokens=self._usage.completion_tokens + usage.completion_tokens,
            total_tokens=self._usage.total_tokens + usage.total_tokens,
            cached_tokens=self._usage.cached_tokens + usage.cached_tokens,
            logical_call_count=self._usage.logical_call_count,
        )


def _read_resource(root: Path, folder: str, filename: str) -> str:
    relative = Path(folder) / filename
    path = root / filename
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ConfigurationError(f"cannot read required file: {relative.as_posix()}") from None
    if not content.strip():
        raise ConfigurationError(f"required file is empty: {relative.as_posix()}")
    return content.strip()


def _validate_task_text(task_text: str) -> str:
    if not isinstance(task_text, str) or not task_text.strip():
        raise InputError("taskText must be a non-empty string")
    return task_text.strip()


def _validate_request_stage(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InputError("requestStage must be a non-empty string when provided")
    return value.strip()


def _validate_skill(skill: SkillName) -> SkillName:
    if not isinstance(skill, SkillName):
        raise InputError("skill must be one of the three SkillName values")
    return skill


def _validate_images(
    images: Sequence[ModelImage],
    skill: SkillName | None,
    *,
    allow_empty_images: bool = False,
) -> tuple[ModelImage, ...]:
    if isinstance(images, (str, bytes)) or not isinstance(images, Sequence):
        raise InputError("images must be a sequence of ModelImage values")
    checked = tuple(images)
    if any(not isinstance(item, ModelImage) for item in checked):
        raise InputError("images must contain only ModelImage values")
    view_ids = [item.view_id for item in checked]
    if len(set(view_ids)) != len(view_ids):
        raise InputError("model image viewId values must be unique")
    if (
        skill is SkillName.TOPOLOGY_RECOGNITION
        and not checked
        and not allow_empty_images
    ):
        raise InputError("topology_recognition requires at least one image")
    if checked and skill is not SkillName.TOPOLOGY_RECOGNITION:
        raise InputError("image input requires the topology_recognition skill")
    return checked


def _messages(
    system_prompt: str, user_text: str, images: Sequence[ModelImage]
) -> list[dict[str, object]]:
    content: list[dict[str, object]] = []
    for model_image in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _encode_image(model_image)},
            }
        )
    content.append({"type": "text", "text": user_text})
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def _request_metadata(
    system_prompt: str,
    user_text: str,
    schema_text: str | None,
    messages: Sequence[Mapping[str, object]],
) -> _RequestMetadata:
    return _RequestMetadata(
        prompt_bytes=len(system_prompt.encode("utf-8")) + len(user_text.encode("utf-8")),
        schema_bytes=0 if schema_text is None else len(schema_text.encode("utf-8")),
        image_bytes=_message_image_bytes(messages),
    )


def _message_image_bytes(messages: Sequence[Mapping[str, object]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "image_url":
                continue
            image_url = item.get("image_url")
            if not isinstance(image_url, Mapping):
                continue
            value = image_url.get("url")
            if not isinstance(value, str) or "," not in value:
                continue
            encoded = value.split(",", 1)[1]
            padding = len(encoded) - len(encoded.rstrip("="))
            total += max(0, (len(encoded) * 3) // 4 - padding)
    return total


def _request_body_bytes(parameters: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            parameters,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InputError(
            f"cannot serialize model request metadata ({type(exc).__name__})"
        ) from None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _encode_image(model_image: ModelImage) -> str:
    if not isinstance(model_image.image, Image.Image) or model_image.image.mode != "RGB":
        raise InputError(
            f"model image {model_image.view_id!r} must remain an RGB Pillow image"
        )
    try:
        buffer = io.BytesIO()
        model_image.image.save(buffer, format="PNG")
    except (AttributeError, OSError, ValueError):
        raise InputError(f"cannot encode model image {model_image.view_id!r}") from None
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _view_description(images: Sequence[ModelImage]) -> str:
    if not images:
        return "## Image views\nNo image views were supplied."
    lines = [
        "## Image views",
        "Coordinates use each listed view's top-left corner as (0, 0).",
    ]
    lines.extend(
        f"- viewId={item.view_id}; width={item.image.width}; height={item.image.height}"
        for item in images
    )
    return "\n".join(lines)


def _structured_user_text(
    task: str,
    images: Sequence[ModelImage],
    skill_prompt: str,
    extraction_prompt: str,
    schema_text: str,
) -> str:
    return "\n\n".join(
        (
            "## Current skill\n" + skill_prompt,
            "## Extraction procedure\n" + extraction_prompt,
            "## Current task\n" + task,
            _view_description(images),
            "## Target JSON Schema\n"
            "Use the camelCase field names exactly as shown. Return one JSON object.\n"
            + schema_text,
        )
    )


def _repair_user_text(
    skill_prompt: str,
    repair_prompt: str,
    original: str,
    error: str,
    schema_text: str,
) -> str:
    return "\n\n".join(
        (
            "## Current skill\n" + skill_prompt,
            "## Repair procedure\n" + repair_prompt,
            "## Original model output\n" + original,
            "## Parsing or validation error\n" + error,
            "## Target JSON Schema\n"
            "Use the camelCase field names exactly as shown. Return one JSON object.\n"
            + schema_text,
        )
    )


def _completion_text(completion: Any) -> str:
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ModelInvocationError("model response has no choices")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise ModelInvocationError("model response choice has no text content")
    return content


def _completion_model(completion: Any, configured_model: str) -> str:
    value = getattr(completion, "model", None)
    return value.strip() if isinstance(value, str) and value.strip() else configured_model


def _completion_request_id(completion: Any) -> str | None:
    value = getattr(completion, "_request_id", None)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:200]


def _completion_finish_reason(completion: Any | None) -> str | None:
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, list) or not choices:
        return None
    value = _attribute(choices[0], "finish_reason")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _completion_usage(completion: Any | None) -> ModelUsage:
    response_usage = getattr(completion, "usage", None)
    details = _attribute(response_usage, "prompt_tokens_details")
    return ModelUsage(
        prompt_tokens=_usage_number(response_usage, "prompt_tokens"),
        completion_tokens=_usage_number(response_usage, "completion_tokens"),
        total_tokens=_usage_number(response_usage, "total_tokens"),
        cached_tokens=_usage_number(details, "cached_tokens"),
    )


def _validate_structured_value(
    raw_text: str,
    response_model: type[T],
    validation_context: Mapping[str, object] | None = None,
) -> T:
    object_text = extract_json_object(raw_text)
    decoded = json.loads(object_text)
    return response_model.model_validate(decoded, context=validation_context)


def _raise_output_error(
    stage: str, exc: ModelInvocationError | ValidationError
) -> None:
    raise ModelInvocationError(f"{stage} failed validation: {_output_error(exc)}") from None


def _output_error(exc: ModelInvocationError | ValidationError) -> str:
    if isinstance(exc, ValidationError):
        details = []
        for error in exc.errors(
            include_url=False, include_context=False, include_input=False
        )[:8]:
            path = ".".join(str(part) for part in error["loc"]) or "<root>"
            details.append(f"{path}: {error['msg']}")
        extra = len(exc.errors()) - len(details)
        if extra > 0:
            details.append(f"{extra} additional validation error(s)")
        return _safe_excerpt("; ".join(details), limit=800)
    return _safe_excerpt(str(exc), limit=800)


def _is_json_object(candidate: str) -> bool:
    if not candidate:
        return False
    try:
        decoded = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(decoded, dict)


def _balanced_object_candidates(text: str):
    for start, character in enumerate(text):
        if character != "{":
            continue
        stack: list[str] = []
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current in "{[":
                stack.append(current)
            elif current in "}]":
                expected = "{" if current == "}" else "["
                if not stack or stack[-1] != expected:
                    break
                stack.pop()
                if not stack:
                    yield text[start : index + 1]
                    break


def _safe_excerpt(text: str, *, limit: int = 240) -> str:
    redacted = _DATA_URL_PATTERN.sub("[image-data-redacted]", text)
    redacted = _AUTH_PATTERN.sub(r"\1[secret-redacted]", redacted)
    redacted = _SECRET_PATTERN.sub("[secret-redacted]", redacted)
    compact = " ".join(redacted.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _attribute(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _usage_number(value: object, name: str) -> int:
    number = _attribute(value, name)
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        return 0
    return number


def _retryable_status_cause(status: object) -> str | None:
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    if status == 429:
        return "http_429"
    if 500 <= status <= 599:
        return "http_5xx"
    return None


def _is_read_timeout(exc: BaseException) -> bool:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, httpx.ReadTimeout):
            return True
        cause = current.__cause__
        if isinstance(cause, BaseException):
            current = cause
            continue
        context = current.__context__
        current = context if isinstance(context, BaseException) else None
    return False


def _usage_delta(current: ModelUsage, previous: ModelUsage) -> ModelUsage:
    return ModelUsage(
        request_count=current.request_count - previous.request_count,
        prompt_tokens=current.prompt_tokens - previous.prompt_tokens,
        completion_tokens=current.completion_tokens - previous.completion_tokens,
        total_tokens=current.total_tokens - previous.total_tokens,
        cached_tokens=current.cached_tokens - previous.cached_tokens,
        logical_call_count=current.logical_call_count - previous.logical_call_count,
    )
