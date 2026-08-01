"""Application and device mapping configuration loading."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from typing_extensions import Annotated

from .models import (
    ConfigurationError,
    PlatformDevType,
    PlatformNodeType,
    SemanticDeviceType,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=_REPOSITORY_ROOT / ".env", override=False)


def _to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class _ConfigModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        loc_by_alias=True,
    )


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TextStageModelConfig(_ConfigModel):
    """Bounded output budget for the image-backed visual text pass."""

    max_tokens: Annotated[StrictInt, Field(gt=0)] = 8192
    degraded_max_tokens: Annotated[StrictInt, Field(gt=0)] = 4096

    @model_validator(mode="after")
    def require_lower_timeout_budget(self) -> "TextStageModelConfig":
        if self.degraded_max_tokens >= self.max_tokens:
            raise ValueError("degradedMaxTokens must be lower than maxTokens")
        return self


class ModelConfig(_ConfigModel):
    base_url: NonEmptyString
    model_name: NonEmptyString
    text_only_model_name: NonEmptyString | None = None
    enable_thinking: bool
    temperature: Annotated[float, Field(ge=0.0, le=2.0)]
    max_tokens: Annotated[int, Field(gt=0)]
    timeout_seconds: Annotated[float, Field(gt=0.0)]
    text_stage: TextStageModelConfig = Field(default_factory=TextStageModelConfig)
    api_key: SecretStr


class PlatformPathsConfig(_ConfigModel):
    login: NonEmptyString
    image_list: NonEmptyString
    flavor_list: NonEmptyString
    topology_import: NonEmptyString


class PlatformConfig(_ConfigModel):
    base_url: NonEmptyString
    paths: PlatformPathsConfig
    success_codes: list[int] = Field(min_length=1)
    timeout_seconds: Annotated[float, Field(gt=0.0)]
    username: SecretStr
    password: SecretStr


class ImageProcessingConfig(_ConfigModel):
    max_long_edge: Annotated[StrictInt, Field(gt=0)]


class DefaultsConfig(_ConfigModel):
    version: NonEmptyString
    mtu: Annotated[int, Field(gt=0)]
    enable_dhcp: bool
    dns: str


class BudgetConfig(_ConfigModel):
    max_model_calls: Annotated[StrictInt, Field(ge=4)]


class RuntimeConfig(_ConfigModel):
    runs_dir: Path

    @field_validator("runs_dir", mode="before")
    @classmethod
    def reject_empty_path(cls, value: object) -> object:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("must be a non-empty path")
        return value


class DeviceMappingEntry(_ConfigModel):
    node_type: PlatformNodeType | None = None
    dev_type: PlatformDevType | None = None
    resource_role: NonEmptyString | None = None
    image_keywords: list[NonEmptyString] = Field(default_factory=list)


class AppConfig(_ConfigModel):
    model: ModelConfig
    platform: PlatformConfig
    image: ImageProcessingConfig
    defaults: DefaultsConfig
    budget: BudgetConfig
    runtime: RuntimeConfig


_ENVIRONMENT_FIELDS = {
    "TOPOLOGY_MODEL_API_KEY": ("model", "apiKey"),
    "TOPOLOGY_PLATFORM_USERNAME": ("platform", "username"),
    "TOPOLOGY_PLATFORM_PASSWORD": ("platform", "password"),
}


def _read_yaml(path: str | Path) -> dict[str, object]:
    config_path = Path(path)
    try:
        content = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration file: {config_path}") from exc

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        raise ConfigurationError(f"cannot parse YAML configuration: {config_path}") from None
    if not isinstance(data, dict):
        raise ConfigurationError(f"configuration root must be an object: {config_path}")
    return data


def _format_validation_error(label: str, exc: ValidationError) -> ConfigurationError:
    details = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        path = ".".join(str(part) for part in error["loc"])
        details.append(f"{path}: {error['msg']}")
    return ConfigurationError(f"invalid {label}: {'; '.join(details)}")


def _inject_secrets(
    data: dict[str, object], environment: Mapping[str, str]
) -> dict[str, object]:
    result = dict(data)
    for section_name in {section for section, _ in _ENVIRONMENT_FIELDS.values()}:
        section = result.get(section_name)
        if section is not None and not isinstance(section, dict):
            continue
        section_data = dict(section or {})
        sensitive_fields = {
            field
            for configured_section, field in _ENVIRONMENT_FIELDS.values()
            if configured_section == section_name
        }
        present = sensitive_fields.intersection(section_data)
        if present:
            paths = ", ".join(f"{section_name}.{field}" for field in sorted(present))
            raise ConfigurationError(
                f"sensitive configuration must come from environment variables: {paths}"
            )
        result[section_name] = section_data

    missing = []
    for variable, (section_name, field_name) in _ENVIRONMENT_FIELDS.items():
        value = environment.get(variable)
        if not isinstance(value, str) or not value.strip():
            missing.append(f"{section_name}.{field_name} ({variable})")
            continue
        section = result[section_name]
        if isinstance(section, dict):
            section[field_name] = value
    if missing:
        raise ConfigurationError(
            f"missing sensitive configuration: {', '.join(missing)}"
        )
    return result


def load_app_config(
    path: str | Path = Path("config/app.yaml"),
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load application YAML and inject the three fixed secret variables."""

    environment = os.environ if environ is None else environ
    data = _inject_secrets(_read_yaml(path), environment)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise _format_validation_error("application configuration", exc) from None


def load_device_mapping(
    path: str | Path = Path("config/device_mapping.yaml"),
) -> dict[SemanticDeviceType, DeviceMappingEntry]:
    """Load and validate the complete semantic device mapping."""

    data = _read_yaml(path)
    adapter = TypeAdapter(dict[SemanticDeviceType, DeviceMappingEntry])
    try:
        mapping = adapter.validate_python(data)
    except ValidationError as exc:
        raise _format_validation_error("device mapping", exc) from None

    missing = set(SemanticDeviceType).difference(mapping)
    if missing:
        names = ", ".join(sorted(item.value for item in missing))
        raise ConfigurationError(f"invalid device mapping: missing entries: {names}")

    unknown = mapping[SemanticDeviceType.UNKNOWN]
    if any((unknown.node_type, unknown.dev_type, unknown.resource_role)):
        raise ConfigurationError(
            "invalid device mapping: unknown platform mapping must be empty"
        )
    for semantic_type, entry in mapping.items():
        if semantic_type is SemanticDeviceType.UNKNOWN:
            continue
        missing_fields = [
            name
            for name, value in (
                ("nodeType", entry.node_type),
                ("devType", entry.dev_type),
                ("resourceRole", entry.resource_role),
            )
            if value is None
        ]
        if missing_fields:
            raise ConfigurationError(
                "invalid device mapping: "
                f"{semantic_type.value}.{', '.join(missing_fields)} is required"
            )
    return mapping
