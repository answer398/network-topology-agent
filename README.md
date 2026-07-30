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

加载配置前设置 `TOPOLOGY_MODEL_API_KEY`、`TOPOLOGY_PLATFORM_USERNAME` 和
`TOPOLOGY_PLATFORM_PASSWORD`。配置和 Payload 可按以下方式解析：

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
- 全局缩放图、重叠切片、原图坐标裁剪和双向坐标映射；
- 供后续模块统一消费的 `ImageBundle`。

Pillow 会随 editable install 一并安装。设置上述三个环境变量后，可加载图片：

```bash
.venv/bin/python - <<'PY'
from topology_agent import load_app_config, load_image_bundle

config = load_app_config("config/app.yaml")
bundle = load_image_bundle("path/to/topology.png", config.image)
print(bundle.image_info, bundle.sha256, len(bundle.tile_views))
PY
```

M3～M8 的模型调用、视觉识别、拓扑推理、平台交互、Payload 编译、验证、编排和提交尚未实现。
