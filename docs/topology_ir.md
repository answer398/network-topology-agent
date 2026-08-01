# Topology Observation 与 Topology IR 规范

## 1. 文档目的

本文定义工程内部使用的三层拓扑表示：

```text
TopologyObservation
  → TopologyIR
  → ResolvedTopologyIR
```

三层结构用于分离：

- 图片识别；
- 对象规范化；
- 网络语义推理；
- 平台 JSON 编译。

本文定义的数据模型集中实现于：

```text
src/topology_agent/models.py
```

不维护一套与代码重复的手写 JSON Schema。需要给多模态模型使用时，由数据模型生成结构化输出 Schema。

---

## 2. 三层结构

## 2.1 TopologyObservation

保存三个完整全图视觉阶段经过程序预融合、一次纯文本语义融合补丁和程序最终强校验后
得到的观察结果。最终对象由程序构造，模型不直接输出完整 `TopologyObservation`。

允许：

- 重复节点；
- 多个文字候选；
- 未确定链路端点；
- 未绑定接口；
- 低置信度；
- 未解析项。

M04 的所有坐标在返回前统一转换为 EXIF 纠正后的原图像素坐标，Evidence 只引用
`global_structure`、`global_links` 和 `global_text`。纯文本 Fusion 不创建图片 Evidence，
不修改坐标，也不执行 CIDR、网关或广播域推理。

## 2.2 TopologyIR

保存程序规范化后的拓扑。

具备：

- 唯一节点；
- 唯一接口；
- 唯一链路；
- 稳定临时 ID；
- 规范化名称；
- 区域成员；
- 明确接口归属。

此层仍不包含平台资源和平台对象 ID。

## 2.3 ResolvedTopologyIR

保存网络推理完成后的拓扑。

在 TopologyIR 基础上增加：

- 广播域；
- CIDR；
- 网关；
- 成员接口；
- 接口所属网段；
- 网络一致性状态。

只有不存在阻塞性未解析项时，才允许进入平台编译。

---

## 3. 平台无关边界

IR 描述：

- 设备；
- 接口；
- IP；
- 链路；
- 广播域；
- 子网；
- 网关；
- 区域；
- 图片位置；
- 证据；
- 不确定性。

IR 不描述：

- `projectId`；
- `networkId`；
- 平台节点 UUID；
- 平台 NIC UUID；
- `imageId`；
- Flavor；
- `Node.type`；
- `properties.devType`；
- `NetworkConfig`；
- `SubnetConfig`；
- 平台请求头和接口。

---

## 4. ID 约定

IR 临时 ID 只要求在一次任务内唯一。

| 对象 | 前缀 |
|---|---|
| 节点 | `node_` |
| 接口 | `if_` |
| 链路 | `link_` |
| 区域 | `region_` |
| 广播域 | `segment_` |
| 证据 | `evidence_` |
| 未解析项 | `unresolved_` |

示例：

```text
node_001
if_001
link_001
segment_001
```

这些 ID 不等于最终平台 ID。

M04 在程序预融合阶段为最终 Observation 分配确定性 ID：

| 对象 | 前缀示例 |
|---|---|
| 节点观察 | `obs_node_001` |
| 接口观察 | `obs_if_001` |
| 链路观察 | `obs_link_001` |
| 区域观察 | `obs_region_001` |
| 图片证据 | `evidence_001` |
| 未解析项 | `unresolved_001` |

四阶段内部冲突使用 `conflict_001` 等稳定 ID，但冲突是 M04 私有融合上下文，不泄漏到
不支持该字段的公开 `TopologyObservation`。以上所有 ID 均只要求任务内唯一，不使用
随机 UUID，也不等于 IR 或平台对象 ID。

---

## 5. 坐标约定

图片对象统一使用原图像素坐标。

```json
{
  "bbox": {
    "x": 100,
    "y": 200,
    "width": 80,
    "height": 60
  },
  "center": {
    "x": 140,
    "y": 230
  }
}
```

约束：

- 原点位于左上角；
- `x` 向右增加；
- `y` 向下增加；
- `global_structure`、`global_links`、`global_text` 的识别坐标必须转换回原图；
- 边界框分别转换左上角和右下角，折线逐点转换，误差不超过 1 像素；
- 平台坐标由编译器另行转换。

---

## 6. 置信度约定

范围：

```text
0.0 ～ 1.0
```

解释：

| 范围 | 含义 |
|---|---|
| `0.90～1.00` | 视觉证据明确 |
| `0.75～0.89` | 较可靠 |
| `0.50～0.74` | 存在歧义 |
| `<0.50` | 不应自动采用 |

置信度不能替代引用、IP 和平台验证。

---

## 7. 证据类型

| 值 | 含义 |
|---|---|
| `VISUAL_TEXT` | 图片文字直接识别 |
| `VISUAL_ICON` | 设备图标识别 |
| `VISUAL_LINE` | 连线和端点识别 |
| `VISUAL_REGION` | 区域框和区域标题识别 |
| `NETWORK_DERIVATION` | 网络规则推导 |
| `MODEL_INFERENCE` | 模型推断但非直接文字 |

平台目录来源不放入视觉 IR，而记录在 ResourceBinding 中。

---

## 8. TopologyObservation

根结构：

```text
TopologyObservation
├── taskId
├── image
├── observedNodes
├── observedLinks
├── observedRegions
├── evidence
├── unresolvedItems
└── summary
```

### 8.1 根字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `taskId` | String | 是 | 任务 ID |
| `image` | ImageInfo | 是 | 原图尺寸、格式和视图信息 |
| `observedNodes` | List | 是 | 节点观察结果 |
| `observedLinks` | List | 是 | 链路观察结果 |
| `observedRegions` | List | 是 | 区域观察结果 |
| `evidence` | List | 是 | 图片证据 |
| `unresolvedItems` | List | 是 | 未解决项 |
| `summary` | Object | 是 | 观察统计 |

---

## 9. ObservedNode

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `observationId` | String | 是 | 观察 ID |
| `rawName` | String/Null | 否 | 图片原始名称 |
| `nameCandidates` | List | 否 | 名称候选 |
| `semanticType` | String | 是 | 设备语义类型 |
| `typeCandidates` | List | 否 | 类型候选 |
| `vendorModel` | String/Null | 否 | 厂商或型号 |
| `bbox` | BoundingBox | 是 | 节点边界框 |
| `center` | Point | 是 | 节点中心 |
| `observedInterfaces` | List | 是 | 接口和 IP 观察 |
| `regionCandidates` | List | 否 | 区域候选 |
| `confidence` | Number | 是 | 节点置信度 |
| `evidenceIds` | List | 是 | 证据引用 |
| `sourceViewIds` | List | 是 | 三个业务全图视图中的真实来源 |

设备语义类型：

```text
switch_l2
switch_l3
router
public_router
firewall
ids
waf
des
client
server
web_server
database_server
mail_server
monitor_server
vpc_server
unknown
```

---

## 10. ObservedInterface

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `observationId` | String | 是 | 观察 ID |
| `rawName` | String/Null | 否 | 原始接口名 |
| `nameCandidates` | List | 否 | 接口名候选 |
| `rawIpText` | String/Null | 否 | IP 原始文字 |
| `ipCandidates` | List | 否 | IP/前缀候选 |
| `labelBBox` | BoundingBox/Null | 否 | 接口文字位置 |
| `ipBBox` | BoundingBox/Null | 否 | IP 文字位置 |
| `nearbyLinkIds` | List | 否 | 邻近链路 |
| `confidence` | Number | 是 | 置信度 |
| `evidenceIds` | List | 是 | 证据 |

允许：

- 只有接口名；
- 只有 IP；
- IP 与接口尚未绑定；
- 同一标签存在两个候选节点。

---

## 11. ObservedLink

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `observationId` | String | 是 | 观察 ID |
| `sourceNodeCandidates` | List | 是 | 起点节点候选 |
| `targetNodeCandidates` | List | 是 | 终点节点候选 |
| `sourceInterfaceCandidates` | List | 否 | 起点接口候选 |
| `targetInterfaceCandidates` | List | 否 | 终点接口候选 |
| `polyline` | List<Point> | 是 | 线条路径 |
| `crossingUncertain` | Boolean | 是 | 是否可能只是交叉 |
| `confidence` | Number | 是 | 置信度 |
| `evidenceIds` | List | 是 | 证据 |

判断规则：

- 区域边框不是链路；
- 线条交叉不自动表示连接；
- 多段折线可组成同一链路；
- 端点必须落到设备主体或明确接口附近。

---

## 12. ObservedRegion

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `observationId` | String | 是 | 区域观察 ID |
| `rawName` | String/Null | 否 | 原始区域名称 |
| `nameCandidates` | List | 否 | 名称候选 |
| `bbox` | BoundingBox | 是 | 区域范围 |
| `fillColor` | String/Null | 否 | 颜色 |
| `memberNodeCandidates` | List | 是 | 成员节点候选 |
| `confidence` | Number | 是 | 置信度 |
| `evidenceIds` | List | 是 | 证据 |

区域不是节点，不进入节点和链路列表。

---

## 13. TopologyIR

根结构：

```text
TopologyIR
├── taskId
├── image
├── nodes
├── interfaces
├── links
├── regions
├── evidence
├── unresolvedItems
└── summary
```

---

## 14. NodeIR

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tempId` | String | 是 | 节点临时 ID |
| `rawName` | String | 是 | 图片原始名称 |
| `normalizedName` | String | 是 | 规范化名称 |
| `semanticType` | String | 是 | 语义类型 |
| `vendorModel` | String/Null | 否 | 厂商或型号 |
| `interfaceIds` | List<String> | 是 | 所属接口 |
| `regionId` | String/Null | 否 | 所属区域 |
| `bbox` | BoundingBox | 是 | 原图位置 |
| `center` | Point | 是 | 原图中心 |
| `resourceRole` | String | 是 | 资源匹配角色 |
| `imageKeywords` | List<String> | 是 | 镜像关键词 |
| `confidence` | Number | 是 | 置信度 |
| `evidenceIds` | List<String> | 是 | 证据 |

名称规范化只允许：

- 去首尾空格；
- 合并换行；
- 统一全角半角；
- 统一明确的接口大小写；
- 修复有充分证据的分隔符。

不得重写节点名称。

---

## 15. InterfaceIR

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tempId` | String | 是 | 接口 ID |
| `nodeId` | String | 是 | 所属节点 |
| `rawName` | String/Null | 否 | 原始接口名 |
| `normalizedName` | String | 是 | 规范化接口名 |
| `ipAddress` | String/Null | 否 | IPv4 地址 |
| `prefixLength` | Integer/Null | 否 | 前缀长度 |
| `linkId` | String/Null | 否 | 对应链路 |
| `segmentId` | String/Null | 否 | 所属广播域 |
| `confidence` | Number | 是 | 置信度 |
| `evidenceIds` | List | 是 | 证据 |

约束：

- 一个接口只属于一个节点；
- 一个接口不能属于多个互斥广播域；
- `ipAddress` 不包含 `/24`；
- 前缀单独保存于 `prefixLength`。

---

## 16. LinkIR

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tempId` | String | 是 | 链路 ID |
| `sourceNodeId` | String | 是 | 起点节点 |
| `targetNodeId` | String | 是 | 终点节点 |
| `sourceInterfaceId` | String/Null | 否 | 起点接口 |
| `targetInterfaceId` | String/Null | 否 | 终点接口 |
| `polyline` | List<Point> | 是 | 原图路径 |
| `confidence` | Number | 是 | 置信度 |
| `evidenceIds` | List | 是 | 证据 |

IR 中链路视为无向关系。

下列两条在端点和路径一致时属于重复：

```text
A → B
B → A
```

---

## 17. RegionIR

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tempId` | String | 是 | 区域 ID |
| `rawName` | String | 是 | 原始名称 |
| `normalizedName` | String | 是 | 规范化名称 |
| `bbox` | BoundingBox | 是 | 区域范围 |
| `memberNodeIds` | List<String> | 是 | 成员节点 |
| `fillColor` | String/Null | 否 | 区域颜色 |
| `confidence` | Number | 是 | 置信度 |
| `evidenceIds` | List | 是 | 证据 |

默认根据节点图标中心判断区域归属。

---

## 18. SegmentIR

SegmentIR 只存在于 ResolvedTopologyIR。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tempId` | String | 是 | 广播域 ID |
| `anchorNodeId` | String | 是 | 通常为二层交换机 |
| `memberNodeIds` | List<String> | 是 | 成员节点 |
| `memberInterfaceIds` | List<String> | 是 | 成员接口 |
| `cidr` | String | 是 | 子网 CIDR |
| `gatewayIp` | String/Null | 否 | 网关 |
| `gatewayInterfaceId` | String/Null | 否 | 网关接口 |
| `vlan` | String/Null | 否 | 图片明确时填写 |
| `nameHint` | String | 是 | 网络命名提示 |
| `confidence` | Number | 是 | 推理置信度 |
| `evidenceIds` | List | 是 | 推导依据 |

默认规则：

- 每个二层交换机建立一个广播域；
- 直接连接的 VM、路由器和防火墙接口加入广播域；
- 交换机自身不生成业务 NIC；
- 交换机互连和点到点链路单独处理。

---

## 19. Evidence

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `evidenceId` | String | 是 | 证据 ID |
| `sourceType` | String | 是 | 证据类型 |
| `sourceViewId` | String/Null | 否 | `global_structure`、`global_links` 或 `global_text` |
| `bbox` | BoundingBox/Null | 否 | 证据位置 |
| `rawText` | String/Null | 否 | 图片原文 |
| `description` | String | 是 | 说明 |
| `confidence` | Number | 是 | 置信度 |

---

## 20. UnresolvedItem

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tempId` | String | 是 | 未解析项 ID |
| `category` | String | 是 | 问题类别 |
| `objectIds` | List<String> | 是 | 相关对象 |
| `field` | String/Null | 否 | 相关字段 |
| `candidates` | List | 否 | 候选值 |
| `reason` | String | 是 | 无法确定原因 |
| `blocking` | Boolean | 是 | 是否阻止提交 |
| `recommendedAction` | String | 是 | 建议处理 |
| `evidenceIds` | List | 是 | 证据 |

类别：

```text
AMBIGUOUS_NODE_NAME
AMBIGUOUS_DEVICE_TYPE
AMBIGUOUS_INTERFACE_NAME
AMBIGUOUS_IP
UNKNOWN_PREFIX
AMBIGUOUS_LINK_ENDPOINT
CROSSING_UNCERTAIN
MULTIPLE_GATEWAY_CANDIDATES
UNSUPPORTED_TOPOLOGY_PATTERN
```

默认阻塞：

- IP 数字不明确；
- 前缀不明确；
- 链路端点不明确；
- 多个网关候选无法消解；
- 网络关系互相冲突。

---

## 21. 规范化规则

### 21.1 节点去重

综合判断：

- 边界框重叠；
- 中心距离；
- 名称；
- 图标类别；
- 来源阶段和完整视图；
- 邻接链路。

不得只按名称去重。

### 21.2 接口绑定

优先级：

1. 接口文字与节点距离；
2. 接口文字与线条端点距离；
3. IP 文字与接口文字距离；
4. 线条方向；
5. 网络地址一致性。

网络一致性只用于消歧，不能覆盖明显视觉证据。

### 21.3 链路去重

综合：

- 无向端点组合；
- 路径重合；
- 接口名称；
- 端点位置。

### 21.4 区域归属

使用节点图标中心，不使用跨区域的文字中心。

---

## 22. 网络推理规则

### 22.1 CIDR

优先级：

1. 图片明确给出 IP/前缀；
2. 同一广播域其他接口明确给出一致前缀；
3. 任务参数明确提供；
4. 无法确认则登记 `UNKNOWN_PREFIX`。

禁止仅根据裸 IP 假设 `/24`。

### 22.2 网关

候选优先级：

1. 路由器接口；
2. 防火墙接口；
3. 三层交换机接口；
4. 图片明确标记的网关。

网关必须：

- 属于 CIDR；
- 对应存在的接口；
- 不与终端地址冲突。

### 22.3 IP 一致性

- 同一广播域无重复 IP；
- IP 属于 CIDR；
- 网关属于 CIDR；
- 一个接口只属于一个广播域；
- 一个接口不能被多条不合理链路复用。

---

## 23. 进入平台编译的条件

ResolvedTopologyIR 必须满足：

- 节点 ID 唯一；
- 接口 ID 唯一；
- 链路 ID 唯一；
- 所有接口引用存在节点；
- 所有链路端点存在；
- 所有 SegmentIR 具有 CIDR；
- 所有需要地址的接口具有合法 IP；
- 网关合法；
- 区域引用完整；
- 不存在阻塞性未解析项。

---

## 24. 简化示例

```json
{
  "taskId": "task_001",
  "nodes": [
    {
      "tempId": "node_sw1",
      "rawName": "Switch1",
      "normalizedName": "Switch1",
      "semanticType": "switch_l2",
      "interfaceIds": [],
      "regionId": null,
      "resourceRole": "switch_l2",
      "imageKeywords": [],
      "confidence": 0.99,
      "evidenceIds": ["evidence_sw1"]
    },
    {
      "tempId": "node_pc1",
      "rawName": "PC1",
      "normalizedName": "PC1",
      "semanticType": "client",
      "interfaceIds": ["if_pc1_eth0"],
      "regionId": null,
      "resourceRole": "client",
      "imageKeywords": ["PC"],
      "confidence": 0.99,
      "evidenceIds": ["evidence_pc1"]
    },
    {
      "tempId": "node_r1",
      "rawName": "Router1",
      "normalizedName": "Router1",
      "semanticType": "router",
      "interfaceIds": ["if_r1_ge0"],
      "regionId": null,
      "resourceRole": "router",
      "imageKeywords": ["router"],
      "confidence": 0.98,
      "evidenceIds": ["evidence_r1"]
    }
  ],
  "interfaces": [
    {
      "tempId": "if_pc1_eth0",
      "nodeId": "node_pc1",
      "rawName": "eth0",
      "normalizedName": "eth0",
      "ipAddress": "192.168.1.2",
      "prefixLength": 24,
      "linkId": "link_sw1_pc1",
      "segmentId": "segment_sw1",
      "confidence": 0.98,
      "evidenceIds": ["evidence_pc1_ip"]
    },
    {
      "tempId": "if_r1_ge0",
      "nodeId": "node_r1",
      "rawName": "GE0/0",
      "normalizedName": "GE0/0",
      "ipAddress": "192.168.1.1",
      "prefixLength": 24,
      "linkId": "link_sw1_r1",
      "segmentId": "segment_sw1",
      "confidence": 0.98,
      "evidenceIds": ["evidence_r1_ip"]
    }
  ],
  "links": [
    {
      "tempId": "link_sw1_pc1",
      "sourceNodeId": "node_sw1",
      "targetNodeId": "node_pc1",
      "sourceInterfaceId": null,
      "targetInterfaceId": "if_pc1_eth0",
      "confidence": 0.99,
      "evidenceIds": ["evidence_link_1"]
    },
    {
      "tempId": "link_sw1_r1",
      "sourceNodeId": "node_sw1",
      "targetNodeId": "node_r1",
      "sourceInterfaceId": null,
      "targetInterfaceId": "if_r1_ge0",
      "confidence": 0.99,
      "evidenceIds": ["evidence_link_2"]
    }
  ],
  "segments": [
    {
      "tempId": "segment_sw1",
      "anchorNodeId": "node_sw1",
      "memberNodeIds": ["node_pc1", "node_r1"],
      "memberInterfaceIds": ["if_pc1_eth0", "if_r1_ge0"],
      "cidr": "192.168.1.0/24",
      "gatewayIp": "192.168.1.1",
      "gatewayInterfaceId": "if_r1_ge0",
      "vlan": null,
      "nameHint": "Switch1",
      "confidence": 1.0,
      "evidenceIds": ["evidence_pc1_ip", "evidence_r1_ip"]
    }
  ],
  "regions": [],
  "unresolvedItems": []
}
```

---

## 25. IR 到平台对象映射

| IR | 平台对象 |
|---|---|
| `NodeIR` | `nodeList[]` |
| `InterfaceIR` | `nodeList[].nicList[]` |
| `LinkIR` | `linkList[]` |
| `SegmentIR` | `networkList[]` 和 `subnetList[]` |
| `RegionIR.normalizedName` | `properties.district` |
| `RegionIR.fillColor` | `properties.fillColor` |
| `NodeIR.semanticType` | `Node.type` 和 `properties.devType` |
| `NodeIR.resourceRole` | 镜像和 Flavor 匹配 |
| `NodeIR.center` | 平台布局坐标输入 |

---

## 26. 样例验收

### 111

ResolvedTopologyIR 期望：

- 9 个节点；
- 8 条链路；
- 3 个 SegmentIR；
- 3 个子网：
  - `192.168.1.0/24`
  - `192.168.2.0/24`
  - `192.168.12.0/24`
- 无区域；
- 无阻塞性未解析项。

### 222

ResolvedTopologyIR 期望：

- 32 个节点；
- 31 条链路；
- 10 个 SegmentIR；
- 10 个子网；
- 4 个区域：
  - `LAB JARINGAN`
  - `RUANG SERVER`
  - `MANAJEMENT`
  - `CLIENT`
- 不生成图片不可见的端口映射、OSPF 和运维描述；
- 无阻塞性未解析项。

---

## 27. 最终原则

Topology IR 必须：

- 准确表达网络语义；
- 保留图片证据；
- 显式记录不确定性；
- 与平台资源分离；
- 与平台对象 ID 分离；
- 能由程序确定性编译；
- 能在错误时指出问题属于视觉、网络还是平台阶段。
