"""Run the existing recognition workflow for the bundled 111 and 222 images."""

from __future__ import annotations

import argparse
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


def _parse_cases() -> tuple[str, ...]:
    parser = argparse.ArgumentParser(
        description="Run topology recognition for bundled 111 and/or 222 images."
    )
    parser.add_argument("cases", nargs="*", choices=tuple(CASES))
    values = parser.parse_args().cases
    return tuple(values) if values else tuple(CASES)


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main() -> int:
    cases = _parse_cases()
    raw = yaml.safe_load((ROOT / "config" / "app.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config/app.yaml must contain an object")
    model_config = ModelConfig.model_validate(
        {**raw["model"], "apiKey": os.environ["TOPOLOGY_MODEL_API_KEY"]}
    )
    image_config = ImageProcessingConfig.model_validate(raw["image"])
    results: list[dict[str, object]] = []

    for task_id in cases:
        image_path = CASES[task_id]
        result: dict[str, object] = {
            "taskId": task_id,
            "imagePath": _display_path(image_path),
        }
        try:
            client = OpenAICompatibleModelClient(
                model_config=model_config,
                api_key=model_config.api_key,
                max_model_calls=4,
            )
            observation = recognize_topology(
                task_id=task_id,
                image_bundle=load_image_bundle(image_path, image_config),
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
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))

    started_at = datetime.now(timezone.utc)
    output_dir = ROOT / "runtime" / "runs"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / (
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
    print(_display_path(output))
    return 0 if all(item["status"] == "succeeded" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
