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

`formatData(obs_data, projectId, networkId)` 与平台登录状态无关。它只接收已经加载的
Observation 字典并返回 Payload，资源匹配使用 `data/list_images.json` 和
`data/list_flavors.json` 中的平台查询快照，不负责 Observation 和 Payload 的文件读写。
设备名称与镜像名称采用包含关系匹配，例如 `Cisco-9000-R2` 可以匹配
`Cisco-9000`。

```python
import json

from api import TopologyPlatformClient

client = TopologyPlatformClient()

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
