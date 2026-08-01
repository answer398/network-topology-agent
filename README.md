# network-topology-agent

## 项目目标

实现单 Agent 的网络拓扑识别链路，将拓扑图片逐步转换为可验证、可导入的平台拓扑数据。

## 权威文档

- `docs/development_plan.md`
- `docs/architecture.md`
- `docs/topology_ir.md`
- `docs/platform_mapping.md`

## 环境安装

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

后续 Python 和 pip 命令必须使用 `.venv/bin/python`。

## 已完成能力

M1 已提供：

- YAML 与固定环境变量配置加载；
- 单次任务输入、Observation、IR、Resolved IR 和资源绑定模型；
- 平台 Payload、验证报告和提交结果模型；
- 由 Pydantic 模型直接生成 JSON Schema。

在仓库根目录的 `.env` 中填写 `TOPOLOGY_MODEL_API_KEY`、
`TOPOLOGY_PLATFORM_USERNAME` 和 `TOPOLOGY_PLATFORM_PASSWORD`，或直接设置同名
shell 环境变量。配置模块会自动加载 `.env`，且不会覆盖 shell 中已有的值。
配置和 Payload 可按以下方式解析：

```python
from pathlib import Path

from topology_agent import PlatformTopologyPayload, load_app_config

config = load_app_config("config/app.yaml")
payload = PlatformTopologyPayload.model_validate_json(
    Path("payload.json").read_text(encoding="utf-8")
)
print(config.model.model_name, payload.version)
```

M2 已提供：

- PNG、JPG、JPEG 加载、EXIF 方向纠正和 RGB 规范化；
- 未增强原图、确定性像素哈希和一次保守增强；
- 覆盖完整拓扑图的 `global_structure` 和 `global_text` 等比例视图；
- `global_links` 的稳定编号标注全图，以及全图视图与原图之间的双向坐标映射；
- 供后续模块统一消费的 `ImageBundle`，不生成切片或局部视图。

Pillow 会随 editable install 一并安装。准备上述三个配置变量后，可加载图片：

```bash
.venv/bin/python - <<'PY'
from topology_agent import load_app_config, load_image_bundle

config = load_app_config("config/app.yaml")
bundle = load_image_bundle("path/to/topology.png", config.image)
print(
    bundle.image_info,
    bundle.sha256,
    bundle.structure_view.view_id,
    bundle.text_enhanced_view.view_id,
)
PY
```

M3 已提供：

- `system.md`、`extraction.md`、`repair.md` 三个固定 Prompt；
- `topology_recognition`、`network_reasoning`、`platform_mapping` 三个固定 Skill，每次调用只加载其中一个；
- `qwen3.7-plus` OpenAI 兼容 Chat Completions 同步调用（模型名从配置读取）；
- Pillow 图片的内存 PNG data URL 编码、JSON mode、本地 Pydantic Schema 校验和一次有限修复；
- 429、502、503、504 有限重试、调用预算以及逻辑调用、HTTP 请求和 Token 统计。

模型密钥固定使用 `TOPOLOGY_MODEL_API_KEY`。项目启动时会自动读取仓库根目录中
已被 `.gitignore` 忽略的 `.env`，无需每次手动执行 `source`：

```bash
cp .env.example .env
# 在 .env 中填写真实值，然后直接运行项目命令。
```

`.env` 不得提交。若同一变量同时存在于 shell 和 `.env`，以 shell 中的值为准。

以下命令使用 `config/app.yaml` 中的真实兼容地址和模型名执行一次文本结构化调用。
它只加载 `network_reasoning`，不要求填写平台凭据：

```bash
.venv/bin/python - <<'PY'
import os

import yaml
from pydantic import BaseModel

from topology_agent import OpenAICompatibleModelClient, SkillName
from topology_agent.config import ModelConfig

with open("config/app.yaml", encoding="utf-8") as stream:
    raw_model = yaml.safe_load(stream)["model"]
model_config = ModelConfig.model_validate(
    {**raw_model, "apiKey": os.environ["TOPOLOGY_MODEL_API_KEY"]}
)

class Probe(BaseModel):
    ok: bool
    note: str

client = OpenAICompatibleModelClient(
    model_config=model_config,
    api_key=model_config.api_key,
    max_model_calls=2,
)
result = client.call_structured(
    task_text="Return ok=true and a short note confirming JSON mode.",
    images=(),
    response_model=Probe,
    skill=SkillName.NETWORK_REASONING,
)
print(result.value, result.usage)
PY
```

M2 的 `ImageView` 可直接构造 M3 图片输入。M4 固定按
`global_structure`、`global_links`、`global_text` 顺序发送三张完整视图，第四次
Fusion 调用只发送结构化文本，不发送图片：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path

import yaml

from topology_agent import ModelImage, load_image_bundle
from topology_agent.config import ImageProcessingConfig

raw = yaml.safe_load(Path("config/app.yaml").read_text(encoding="utf-8"))
bundle = load_image_bundle(
    "path/to/topology.png",
    ImageProcessingConfig.model_validate(raw["image"]),
)
view = bundle.structure_view
model_image = ModelImage(view_id=view.view_id, image=view.image)
print(model_image.view_id, model_image.image.size)
PY
```

M4 已提供固定四阶段识别：结构全图、节点编号标注链路全图、文字增强全图，以及
Qwen3.7-Plus 纯文本语义融合。程序负责预融合、补丁校验、最终
`TopologyObservation` 构造和几何、证据、引用及 Schema 强校验；M5～M8 的
拓扑推理、平台交互、Payload 编译、验证、编排和提交仍未实现。

识别运行会为每次执行创建 `runtime/runs/<taskId>/attempt_###/`。目录内的
`recognition.jsonl` 记录逐阶段事件、真实 HTTP/Token 统计和失败类别；三个视觉阶段分别保存
实际使用的完整视图 `global_structure.png`、`global_links.png` 和 `global_text.png`，用于核对
二次渲染和模型输入。每次请求发送前还会写入对应的 `*_context.json`，其中只有 task、视图
坐标关系和程序生成的紧凑结构化摘要；它不包含 API Key、图片 data URL、完整 Prompt、Schema、
原始模型响应或 reasoning。

视觉阶段成功后会写入 `structure_evidence.json`、`links_evidence.json`、
`text_evidence.json`。Fusion 是纯文本阶段，保存 `fusion_context.json` 与结构化
`fusion_patch.json`，不生成或发送第四张图片。最终通过全部强校验后才写入
`topology_observation.json`。这些产物用于审计和定位失败，不是响应缓存；重跑会创建新 attempt
并重新执行四次业务逻辑调用。
