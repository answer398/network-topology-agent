"""Run the existing recognition workflow for the bundled 111 and 222 images."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from topology_agent import (
    OpenAICompatibleModelClient,
    load_image_bundle,
    recognize_topology,
)
from topology_agent.config import ImageProcessingConfig, ModelConfig


ROOT = Path(__file__).resolve().parent
CASES = {
    "111": ROOT / "runtime" / "runs" / "111.png",
    "222": ROOT / "runtime" / "runs" / "222_v1.png",
}


def _latest_attempt(task_id: str, before: set[Path]) -> Path | None:
    task_dir = ROOT / "runtime" / "runs" / task_id
    current = {path for path in task_dir.glob("attempt_*") if path.is_dir()}
    created = sorted(current - before)
    return created[-1] if created else (max(current, default=None, key=lambda path: path.name))


def _relative(path: Path | None) -> str | None:
    return None if path is None else path.resolve().relative_to(ROOT).as_posix()


def main() -> int:
    config_path = ROOT / "config" / "app.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    model = ModelConfig.model_validate(
        {**raw["model"], "apiKey": os.environ["TOPOLOGY_MODEL_API_KEY"]}
    )
    image_config = ImageProcessingConfig.model_validate(raw["image"])
    results: list[dict[str, object]] = []

    for task_id, image_path in CASES.items():
        task_dir = ROOT / "runtime" / "runs" / task_id
        before = {path for path in task_dir.glob("attempt_*") if path.is_dir()}
        client: OpenAICompatibleModelClient | None = None
        result: dict[str, object] = {"taskId": task_id, "imagePath": _relative(image_path)}
        try:
            bundle = load_image_bundle(image_path, image_config)
            client = OpenAICompatibleModelClient(
                model_config=model,
                api_key=model.api_key,
                max_model_calls=4,
            )
            observation = recognize_topology(
                task_id=task_id,
                image_bundle=bundle,
                model_client=client,
            )
            result.update(
                status="succeeded",
                usage={
                    "logicalCalls": client.usage.logical_call_count,
                    "httpRequests": client.usage.request_count,
                    "inputTokens": client.usage.prompt_tokens,
                    "outputTokens": client.usage.completion_tokens,
                },
                observationSummary=observation.summary,
            )
        except Exception as exc:
            result.update(status="failed", errorType=type(exc).__name__)
        finally:
            close = getattr(getattr(client, "_http_client", None), "close", None)
            if callable(close):
                close()

        attempt = _latest_attempt(task_id, before)
        result["attemptDirectory"] = _relative(attempt)
        result["recognitionLog"] = _relative(
            None if attempt is None else attempt / "recognition.jsonl"
        )
        results.append(result)
        print(f"[{task_id}] {result['status']} log={result['recognitionLog'] or '-'}")

    started_at = datetime.now(timezone.utc)
    output = ROOT / "runtime" / "runs" / (
        "sample_recognition_summary_" + started_at.strftime("%Y%m%dT%H%M%S.%fZ") + ".json"
    )
    output.write_text(
        json.dumps(
            {"createdAt": started_at.isoformat(timespec="milliseconds"), "cases": results},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"summary={_relative(output)}")
    return 0 if all(item["status"] == "succeeded" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
