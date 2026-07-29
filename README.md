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

当前仅完成工程初始化，业务模块尚未实现。后续 Python 和 pip 命令必须使用 `.venv/bin/python`。
