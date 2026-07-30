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

## M1 能力

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

M2 及后续图片处理、模型调用、拓扑推理、平台交互、编译和编排功能尚未实现。
