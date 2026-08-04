# API 使用说明

```python
from api import TopologyPlatformClient
import json

client = TopologyPlatformClient()

client.login("dlf09", "1qaz@WSX")

with open("./data/111.json", "r", encoding="utf-8") as f:
    payload = json.load(f)

client.import_topology(payload)
client.close()
```

## 格式化识别结果

`formatData(obs_data, projectId, networkId)` 接收已经加载的 Observation 字典并返回
Payload。生产调用应先登录平台，使适配层从当前平台分页查询镜像和 Flavor；也可以通过
`image_items` 和 `flavor_items` 显式传入已经获取的资源目录。它不负责 Observation 和
Payload 的文件读写。设备名称与镜像名称采用包含关系匹配，例如 `Cisco-9000-R2` 可以
匹配 `Cisco-9000`。

```python
import json

from api import TopologyPlatformClient

client = TopologyPlatformClient()
client.login("<平台用户名>", "<平台密码>")

with open(
    "runtime/runs/111/attempt_001/topology_observation.json",
    encoding="utf-8",
) as stream:
    obs_data = json.load(stream)

payload = client.formatData(
    obs_data,
    projectId="<平台工程 ID>",
    networkId="<平台根网络 ID>",
)

with open("runtime/runs/111/attempt_001/payload.json", "w", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, indent=2)
```

## 离线资源快照

CLI 识别阶段固定不登录平台，直接使用仓库 `data/` 目录中的镜像和 Flavor 快照。快照可以是数组，
也可以是平台响应常见的 `items` 或 `data.items` 结构：

```python
from api import TopologyPlatformClient, load_resource_snapshot

client = TopologyPlatformClient()
images, flavors = load_resource_snapshot(
    "data/list_images.json",
    "data/list_flavors.json",
)
payload = client.formatData(
    obs_data,
    projectId=None,
    networkId=None,
    image_items=images,
    flavor_items=flavors,
)
client.close()
```

需要获取最新快照时执行：

```bash
.venv/bin/python topology_cli.py --refresh-resources
```

该命令在线登录并分页获取镜像和 Flavor，覆盖写入
`data/list_images.json` 与 `data/list_flavors.json`，不会提交拓扑。
真正提交平台时仍需要平台登录和外部工程 ID。
