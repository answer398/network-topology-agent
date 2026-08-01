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