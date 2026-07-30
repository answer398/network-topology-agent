"""OpenAI-compatible multimodal calls with local structured validation."""

from __future__ import annotations

import base64
import io
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, TypeVar
from urllib.parse import urlsplit

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
_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
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
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


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


@dataclass(frozen=True, slots=True)
class RawModelResult:
    raw_text: str
    model: str
    request_id: str | None
    attempts: int
    usage: ModelUsage
    loaded_skill: SkillName | None


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
        self._temperature = model_config.temperature
        self._max_tokens = model_config.max_tokens
        self._timeout_seconds = model_config.timeout_seconds
        self._max_model_calls = max_model_calls
        self._prompt_root = Path(prompt_root)
        self._skill_root = Path(skill_root)
        self._prompt_cache: dict[str, str] = {}
        self._skill_cache: dict[SkillName, str] = {}
        self._usage = ModelUsage()

        secret_value = api_key.get_secret_value()
        if not secret_value.strip():
            raise ConfigurationError("apiKey must not be empty")
        try:
            self._client = OpenAI(
                api_key=secret_value,
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                max_retries=0,
            )
        except Exception as exc:
            raise ConfigurationError(
                f"cannot create OpenAI-compatible client ({type(exc).__name__})"
            ) from None
        finally:
            del secret_value

    def __repr__(self) -> str:
        host = urlsplit(self._base_url).hostname or "<invalid>"
        return (
            f"{type(self).__name__}(model={self._model_name!r}, host={host!r}, "
            f"requests={self._usage.request_count}, budget={self._max_model_calls})"
        )

    @property
    def usage(self) -> ModelUsage:
        """Return the immutable cumulative usage snapshot for this client."""

        return self._usage

    def call_structured(
        self,
        *,
        task_text: str,
        images: Sequence[ModelImage],
        response_model: type[T],
        skill: SkillName,
        allow_repair: bool = True,
    ) -> ModelCallResult[T]:
        """Call JSON mode and validate the final object with a Pydantic model."""

        task = _validate_task_text(task_text)
        checked_skill = _validate_skill(skill)
        checked_images = _validate_images(images, checked_skill)
        if not isinstance(allow_repair, bool):
            raise InputError("allowRepair must be a boolean")
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
        system_prompt = self._prompt("system.md")
        skill_prompt = self._skill(checked_skill)
        extraction_prompt = self._prompt("extraction.md")
        user_text = _structured_user_text(
            task, checked_images, skill_prompt, extraction_prompt, schema_text
        )
        completion = self._request(
            _messages(system_prompt, user_text, checked_images), json_mode=True
        )
        raw_text = _completion_text(completion)
        final_completion = completion
        repaired = False

        try:
            value = _validate_structured_value(raw_text, response_model)
        except (ModelInvocationError, ValidationError) as exc:
            if not allow_repair:
                _raise_output_error("model output", exc)
            repair_prompt = self._prompt("repair.md")
            repair_text = _repair_user_text(
                skill_prompt, repair_prompt, raw_text, _output_error(exc), schema_text
            )
            final_completion = self._request(
                _messages(system_prompt, repair_text, ()), json_mode=True
            )
            raw_text = _completion_text(final_completion)
            repaired = True
            try:
                value = _validate_structured_value(raw_text, response_model)
            except (ModelInvocationError, ValidationError) as repair_exc:
                _raise_output_error("model repair output", repair_exc)

        usage = _usage_delta(self.usage, before)
        return ModelCallResult(
            value=value,
            raw_text=raw_text,
            model=_completion_model(final_completion, self._model_name),
            request_id=_completion_request_id(final_completion),
            attempts=usage.request_count,
            usage=usage,
            loaded_skill=checked_skill,
            repaired=repaired,
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
        completion = self._request(
            _messages(system_prompt, "\n\n".join(part for part in parts if part), checked_images),
            json_mode=False,
        )
        raw_text = _completion_text(completion)
        usage = _usage_delta(self.usage, before)
        return RawModelResult(
            raw_text=raw_text,
            model=_completion_model(completion, self._model_name),
            request_id=_completion_request_id(completion),
            attempts=usage.request_count,
            usage=usage,
            loaded_skill=checked_skill,
        )

    def _prompt(self, filename: str) -> str:
        if filename not in self._prompt_cache:
            self._prompt_cache[filename] = load_prompt(filename, self._prompt_root)
        return self._prompt_cache[filename]

    def _skill(self, skill: SkillName) -> str:
        if skill not in self._skill_cache:
            self._skill_cache[skill] = load_skill(skill, self._skill_root)
        return self._skill_cache[skill]

    def _request(
        self, messages: list[dict[str, object]], *, json_mode: bool
    ) -> Any:
        for retry_index in range(len(_HTTP_RETRY_DELAYS) + 1):
            self._consume_request_budget()
            parameters: dict[str, object] = {
                "model": self._model_name,
                "messages": messages,
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
            }
            if json_mode:
                parameters["response_format"] = {"type": "json_object"}
            try:
                completion = self._client.chat.completions.create(**parameters)
            except APIStatusError as exc:
                status = exc.status_code
                can_retry = (
                    status in _RETRYABLE_STATUS_CODES
                    and retry_index < len(_HTTP_RETRY_DELAYS)
                )
                if can_retry:
                    if self._usage.request_count >= self._max_model_calls:
                        raise self._budget_error() from None
                    time.sleep(_HTTP_RETRY_DELAYS[retry_index])
                    continue
                raise ModelInvocationError(
                    f"model request failed with HTTP {status} ({type(exc).__name__})"
                ) from None
            except APITimeoutError as exc:
                raise ModelInvocationError(
                    f"model request timed out after {self._timeout_seconds:g} seconds "
                    f"({type(exc).__name__})"
                ) from None
            except APIConnectionError as exc:
                raise ModelInvocationError(
                    f"model network request failed ({type(exc).__name__})"
                ) from None
            except APIError as exc:
                raise ModelInvocationError(
                    f"model SDK request failed ({type(exc).__name__})"
                ) from None
            except Exception as exc:
                raise ModelInvocationError(
                    f"unexpected model client failure ({type(exc).__name__})"
                ) from None
            self._record_response_usage(completion)
            return completion
        raise ModelInvocationError("model request retry loop ended unexpectedly")

    def _consume_request_budget(self) -> None:
        if self._usage.request_count >= self._max_model_calls:
            raise self._budget_error()
        self._usage = ModelUsage(
            request_count=self._usage.request_count + 1,
            prompt_tokens=self._usage.prompt_tokens,
            completion_tokens=self._usage.completion_tokens,
            total_tokens=self._usage.total_tokens,
            cached_tokens=self._usage.cached_tokens,
        )

    def _budget_error(self) -> ModelInvocationError:
        return ModelInvocationError(
            f"model call budget exhausted: {self._usage.request_count}/"
            f"{self._max_model_calls} requests used"
        )

    def _record_response_usage(self, completion: Any) -> None:
        response_usage = getattr(completion, "usage", None)
        details = _attribute(response_usage, "prompt_tokens_details")
        self._usage = ModelUsage(
            request_count=self._usage.request_count,
            prompt_tokens=self._usage.prompt_tokens
            + _usage_number(response_usage, "prompt_tokens"),
            completion_tokens=self._usage.completion_tokens
            + _usage_number(response_usage, "completion_tokens"),
            total_tokens=self._usage.total_tokens
            + _usage_number(response_usage, "total_tokens"),
            cached_tokens=self._usage.cached_tokens
            + _usage_number(details, "cached_tokens"),
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


def _validate_skill(skill: SkillName) -> SkillName:
    if not isinstance(skill, SkillName):
        raise InputError("skill must be one of the three SkillName values")
    return skill


def _validate_images(
    images: Sequence[ModelImage], skill: SkillName | None
) -> tuple[ModelImage, ...]:
    if isinstance(images, (str, bytes)) or not isinstance(images, Sequence):
        raise InputError("images must be a sequence of ModelImage values")
    checked = tuple(images)
    if any(not isinstance(item, ModelImage) for item in checked):
        raise InputError("images must contain only ModelImage values")
    view_ids = [item.view_id for item in checked]
    if len(set(view_ids)) != len(view_ids):
        raise InputError("model image viewId values must be unique")
    if skill is SkillName.TOPOLOGY_RECOGNITION and not checked:
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


def _validate_structured_value(raw_text: str, response_model: type[T]) -> T:
    object_text = extract_json_object(raw_text)
    decoded = json.loads(object_text)
    return response_model.model_validate(decoded)


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


def _usage_delta(current: ModelUsage, previous: ModelUsage) -> ModelUsage:
    return ModelUsage(
        request_count=current.request_count - previous.request_count,
        prompt_tokens=current.prompt_tokens - previous.prompt_tokens,
        completion_tokens=current.completion_tokens - previous.completion_tokens,
        total_tokens=current.total_tokens - previous.total_tokens,
        cached_tokens=current.cached_tokens - previous.cached_tokens,
    )
