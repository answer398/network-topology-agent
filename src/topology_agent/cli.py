"""Small command-line orchestration for recognition and topology submission."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
import requests

from api import TopologyPlatformClient, load_resource_snapshot, validate_payload

from .config import AppConfig, load_app_config
from .image import load_image_bundle
from .llm import OpenAICompatibleModelClient
from .models import (
    ConfigurationError,
    PlatformTopologyPayload,
    SubmissionResult,
    TaskStatus,
)
from .recognition import recognize_topology


_ROOT = Path(__file__).resolve().parents[2]
_TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


class _RunLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.sequence = self._last_sequence()

    def _last_sequence(self) -> int:
        if not self.path.is_file():
            return 0
        last = 0
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = record.get("sequence") if isinstance(record, Mapping) else None
                if isinstance(value, int) and not isinstance(value, bool):
                    last = max(last, value)
        except OSError:
            return 0
        return last

    def write(self, event: str, **values: object) -> None:
        self.sequence += 1
        record: dict[str, object] = {
            "sequence": self.sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            **values,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.refresh_resources:
            return _refresh_resources()
        if arguments.recognize is not None:
            return _recognize(arguments.recognize)
        return _submit(arguments.submit[0], arguments.submit[1:])
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recognize topology images and submit generated platform payloads."
    )
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument(
        "-refresh-resources",
        "--refresh-resources",
        dest="refresh_resources",
        action="store_true",
        help="fetch the latest platform image and flavor lists into data/",
    )
    actions.add_argument(
        "-recognize",
        "--recognize",
        dest="recognize",
        metavar="IMAGE_NAME",
        help="recognize runtime/topo/IMAGE_NAME and write runtime/IMAGE_NAME/result.json",
    )
    actions.add_argument(
        "-submit",
        "--submit",
        nargs=3,
        metavar=("IMAGE_NAME", "projectId=VALUE", "networkId=VALUE"),
        help="submit an existing result with the two external platform IDs",
    )
    return parser


def _recognize(task_name: str) -> int:
    task_name = _validate_task_name(task_name)
    image_path = _find_image(task_name)
    output_dir = _ROOT / "runtime" / task_name
    log_dir = output_dir / "log"
    output_dir.mkdir(parents=True, exist_ok=True)
    run_log = _RunLog(log_dir / "run.log")
    run_log.write("recognitionStarted", taskId=task_name, imagePath=str(image_path))

    platform_client: TopologyPlatformClient | None = None
    try:
        config = load_app_config(_ROOT / "config" / "app.yaml")
        bundle = load_image_bundle(image_path, config.image)

        images, flavors = _load_offline_resources(
            config, run_log, reason="recognition"
        )
        platform_client = TopologyPlatformClient()
        if config.model.api_key is None:
            raise ConfigurationError(
                "missing sensitive configuration: model.apiKey "
                "(TOPOLOGY_MODEL_API_KEY)"
            )

        model_client = OpenAICompatibleModelClient(
            model_config=config.model,
            api_key=config.model.api_key,
            max_model_calls=config.budget.max_model_calls,
        )
        observation = recognize_topology(
            task_id=task_name,
            image_bundle=bundle,
            model_client=model_client,
            artifact_dir=log_dir,
        )
        payload = platform_client.formatData(
            observation.model_dump(mode="json", by_alias=True),
            None,
            None,
            image_items=images,
            flavor_items=flavors,
        )
        _validate_result_template(payload)
        result_path = output_dir / "result.json"
        _write_json(result_path, payload)
        run_log.write(
            "recognitionSucceeded",
            resultPath=str(result_path),
            nodeCount=len(payload["nodeList"]),
            linkCount=len(payload["linkList"]),
            networkCount=len(payload["networkList"]),
            subnetCount=len(payload["subnetList"]),
            externalIdsBound=False,
        )
        print(f"recognized: {result_path}")
        return 0
    except Exception as exc:
        run_log.write("recognitionFailed", errorType=type(exc).__name__)
        raise
    finally:
        if platform_client is not None:
            platform_client.close()


def _refresh_resources() -> int:
    output_dir = _ROOT / "runtime" / "resource_refresh"
    run_log = _RunLog(output_dir / "log" / "run.log")
    run_log.write("resourceRefreshStarted")
    platform_client: TopologyPlatformClient | None = None
    try:
        config = load_app_config(
            _ROOT / "config" / "app.yaml",
            require_model_api_key=False,
        )
        username = config.platform.username
        password = config.platform.password
        if username is None or password is None:
            raise ConfigurationError(
                "resource refresh requires TOPOLOGY_PLATFORM_USERNAME and "
                "TOPOLOGY_PLATFORM_PASSWORD"
            )
        image_path = _resolve_data_snapshot_path(
            config.platform.offline_image_file, "image"
        )
        flavor_path = _resolve_data_snapshot_path(
            config.platform.offline_flavor_file, "flavor"
        )
        platform_client = TopologyPlatformClient()
        platform_client.login(
            username.get_secret_value(), password.get_secret_value()
        )
        images = platform_client.list_images()
        flavors = platform_client.list_flavors()
        _write_resource_snapshot(image_path, images)
        _write_resource_snapshot(flavor_path, flavors)
        run_log.write(
            "resourceRefreshSucceeded",
            imageFile=str(image_path),
            flavorFile=str(flavor_path),
            imageCount=len(images),
            flavorCount=len(flavors),
        )
        print(f"resources refreshed: {image_path}, {flavor_path}")
        return 0
    except Exception as exc:
        run_log.write("resourceRefreshFailed", errorType=type(exc).__name__)
        raise
    finally:
        if platform_client is not None:
            platform_client.close()


def _submit(task_name: str, bindings: Sequence[str]) -> int:
    task_name = _validate_task_name(task_name)
    project_id = _parse_binding(bindings, "projectId")
    network_id = _parse_binding(bindings, "networkId")
    output_dir = _ROOT / "runtime" / task_name
    result_path = output_dir / "result.json"
    run_log = _RunLog(output_dir / "log" / "run.log")
    payload_hash = "unavailable"

    try:
        raw_payload = _read_json_object(result_path)
        payload_hash = _payload_hash(raw_payload)
        payload = _bind_external_ids(raw_payload, project_id, network_id)
        typed_payload = PlatformTopologyPayload.model_validate(payload)
        payload = typed_payload.model_dump(mode="json", by_alias=True)
        validate_payload(payload)
        payload_hash = _payload_hash(payload)
    except (OSError, ValueError, ValidationError, TypeError) as exc:
        result = _submission_result(
            payload_hash=payload_hash,
            status=TaskStatus.VALIDATION_FAILED,
            message=f"payload validation failed: {type(exc).__name__}",
        )
        _write_submission_artifacts(output_dir, result, None)
        run_log.write("submissionValidationFailed", errorType=type(exc).__name__)
        print(f"error: {result['message']}", file=sys.stderr)

        return 1

    try:
        config = load_app_config(
            _ROOT / "config" / "app.yaml",
            require_model_api_key=False,
        )
        if config.platform.username is None or config.platform.password is None:
            raise ConfigurationError(
                "submission requires TOPOLOGY_PLATFORM_USERNAME and "
                "TOPOLOGY_PLATFORM_PASSWORD"
            )
        username = config.platform.username.get_secret_value()
        password = config.platform.password.get_secret_value()
    except ConfigurationError as exc:
        result = _submission_result(
            payload_hash=payload_hash,
            status=TaskStatus.FAILED,
            message=f"submission configuration failed: {exc}",
        )
        _write_submission_artifacts(output_dir, result, None)
        run_log.write("submissionConfigurationFailed", errorType=type(exc).__name__)
        print(f"error: {result['message']}", file=sys.stderr)
        return 1
    except Exception as exc:
        result = _submission_result(
            payload_hash=payload_hash,
            status=TaskStatus.FAILED,
            message=f"could not load submission configuration: {type(exc).__name__}",
        )
        _write_submission_artifacts(output_dir, result, None)
        run_log.write("submissionConfigurationFailed", errorType=type(exc).__name__)
        print(f"error: {result['message']}", file=sys.stderr)
        return 1

    try:
        _write_json(result_path, payload)
        run_log.write(
            "submissionPrepared",
            resultPath=str(result_path),
            projectId=project_id,
            networkId=network_id,
            payloadSha256=payload_hash,
        )
    except OSError as exc:
        result = _submission_result(
            payload_hash=payload_hash,
            status=TaskStatus.FAILED,
            message="could not persist the bound payload",
        )
        _write_submission_artifacts(output_dir, result, None)
        run_log.write("submissionFailed", errorType=type(exc).__name__)
        print(f"error: {result['message']}", file=sys.stderr)
        return 1

    platform_client = TopologyPlatformClient()
    logged_in = False
    try:
        platform_client.login(username, password)
        logged_in = True
        response = platform_client.import_topology(payload)
    except TimeoutError as exc:
        result = _submission_result(
            payload_hash=payload_hash,
            status=TaskStatus.SUBMISSION_UNCERTAIN,
            message="topology write outcome is uncertain; server state is unknown",
        )
        _write_submission_artifacts(output_dir, result, None)
        run_log.write("submissionUncertain", errorType=type(exc).__name__)
        print(f"error: {result['message']}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        message = (
            "platform login failed; topology write was not attempted"
            if not logged_in
            else "platform request failed after login; inspect server state before retrying"
        )
        result = _submission_result(
            payload_hash=payload_hash,
            status=TaskStatus.FAILED,
            message=message,
        )
        _write_submission_artifacts(output_dir, result, None)
        run_log.write(
            "platformLoginFailed" if not logged_in else "platformRequestFailed",
            errorType=type(exc).__name__,
        )
        print(f"error: {result['message']}", file=sys.stderr)
        return 1
    except Exception as exc:
        result = _submission_result(
            payload_hash=payload_hash,
            status=TaskStatus.FAILED,
            message=f"topology submission failed: {type(exc).__name__}",
        )
        _write_submission_artifacts(output_dir, result, None)
        run_log.write("submissionFailed", errorType=type(exc).__name__)
        print(f"error: {result['message']}", file=sys.stderr)
        return 1
    finally:
        platform_client.close()

    result = _submission_result(
        payload_hash=payload_hash,
        status=TaskStatus.COMPLETED,
        message=_response_message(response),
        business_code=_response_code(response),
    )
    _write_submission_artifacts(output_dir, result, response)
    run_log.write(
        "submissionSucceeded",
        payloadSha256=payload_hash,
        businessCode=_response_code(response),
    )
    print(f"submitted: {result_path}")
    return 0


def _load_offline_resources(
    config: AppConfig, run_log: _RunLog, reason: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    image_path = _resolve_data_snapshot_path(
        config.platform.offline_image_file, "image"
    )
    flavor_path = _resolve_data_snapshot_path(
        config.platform.offline_flavor_file, "flavor"
    )
    images, flavors = load_resource_snapshot(image_path, flavor_path)
    run_log.write(
        "offlineResourcesLoaded",
        reason=reason,
        imageFile=str(image_path),
        flavorFile=str(flavor_path),
        imageCount=len(images),
        flavorCount=len(flavors),
    )
    return images, flavors


def _resolve_repository_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else _ROOT / path


def _resolve_data_snapshot_path(value: str | Path, label: str) -> Path:
    data_root = (_ROOT / "data").resolve()
    path = _resolve_repository_path(value).resolve()
    try:
        path.relative_to(data_root)
    except ValueError as exc:
        raise ConfigurationError(
            f"{label} resource snapshot must be under data/"
        ) from exc
    return path


def _write_resource_snapshot(
    path: Path, items: Sequence[Mapping[str, object]]
) -> None:
    if any(not isinstance(item, Mapping) for item in items):
        raise TypeError(f"resource snapshot must contain {path.name} objects")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(list(items), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except (OSError, TypeError, ValueError):
        temporary_path.unlink(missing_ok=True)
        raise


def _validate_task_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("image name must be a string")
    task_name = value.strip()
    if not _TASK_NAME.fullmatch(task_name):
        raise ValueError("image name must be a simple file stem")
    if Path(task_name).suffix.lower() in _IMAGE_SUFFIXES:
        return Path(task_name).stem
    return task_name


def _find_image(task_name: str) -> Path:
    topo_dir = _ROOT / "runtime" / "topo"
    supplied = Path(task_name)
    if supplied.suffix.lower() in _IMAGE_SUFFIXES:
        candidates = [topo_dir / task_name]
    else:
        candidates = [
            path
            for path in sorted(topo_dir.glob(f"{task_name}.*"))
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
        ]
    existing = [path for path in candidates if path.is_file()]
    if len(existing) != 1:
        if not existing:
            raise ValueError(f"no PNG/JPG image found for {task_name!r} in {topo_dir}")
        raise ValueError(f"multiple images found for {task_name!r} in {topo_dir}")
    return existing[0]


def _parse_binding(bindings: Sequence[str], expected: str) -> str:
    matches = [
        value.split("=", 1)[1].strip()
        for value in bindings
        if value.startswith(expected + "=")
    ]
    if len(matches) != 1 or not matches[0]:
        raise ValueError(f"submit requires exactly one non-empty {expected}=VALUE")
    return matches[0]


def _bind_external_ids(
    payload: Mapping[str, Any], project_id: str, network_id: str
) -> dict[str, Any]:
    result = dict(payload)
    for field, value in (("projectId", project_id), ("networkId", network_id)):
        existing = result.get(field)
        if existing is not None and existing != value:
            raise ValueError(f"result.json {field} does not match submit input")
        result[field] = value
    return result


def _validate_result_template(payload: Mapping[str, Any]) -> None:
    required = ("networkList", "subnetList", "nodeList", "linkList", "portMappingList")
    if any(not isinstance(payload.get(field), list) for field in required):
        raise ValueError("result template is missing a platform collection")
    if "projectId" in payload or "networkId" in payload:
        raise ValueError("recognition result must not contain external platform IDs")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"result must be a JSON object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _submission_result(
    *,
    payload_hash: str,
    status: TaskStatus,
    message: str,
    business_code: int | None = None,
) -> dict[str, Any]:
    return SubmissionResult(
        business_code=business_code,
        message=message,
        payload_hash=payload_hash,
        task_status=status,
        write_succeeded=status is TaskStatus.COMPLETED,
    ).model_dump(mode="json", by_alias=True)


def _write_submission_artifacts(
    output_dir: Path,
    result: Mapping[str, Any],
    response: Mapping[str, Any] | None,
) -> None:
    _write_json(output_dir / "submission_result.json", result)
    if response is not None:
        _write_json(output_dir / "submission_response.json", _redact(response))


def _response_code(response: Mapping[str, Any]) -> int | None:
    value = response.get("code")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _response_message(response: Mapping[str, Any]) -> str:
    value = response.get("message")
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())[:300]
    return "topology import succeeded"


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and key.casefold() in {
        "authorization",
        "token",
        "accesstoken",
        "password",
        "apikey",
        "api_key",
    }:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(name): _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return value[:2000]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
