# 网络拓扑识别 Agent 开发计划

> 目标：在交付周期和工程复杂度受限的条件下，实现一条稳定的“拓扑图片 → Topology IR → 平台 JSON → 标准接口导入”链路。
>
> 约束：
>
> - 使用一个多模态 Agent；
> - 保留三个 Skill：`topology_recognition`、`network_reasoning`、`platform_mapping`；
> - `projectId`、`networkId` 由操作人员在平台界面创建工程后取得，并作为任务输入；
> - 不建立 `tests/` 目录，不使用 pytest，不开发测试辅助脚本；
> - 每个模块直接在本文中定义测试内容和期望结果；
> - 不实现多 Agent、复杂服务化、多供应商抽象、完整回放系统和过度细分的代码层次；
> - 优先保证最终 Payload 正确和平台导入成功。

---

## 1. 实现范围

## 1.1 核心功能

```text
图片输入
  → 图片预处理
  → 多模态模型结构化识别
  → Topology IR
  → 网络语义推理
  → 平台镜像和 Flavor 查询
  → 平台 JSON 编译
  → 提交前验证
  → 标准拓扑导入
```

必须覆盖：

- 路由器；
- 二层交换机；
- 三层交换机；
- 防火墙；
- 客户端；
- 服务器；
- 节点名称；
- 接口名称；
- IPv4 和前缀；
- 节点间链路；
- 广播域；
- 子网和网关；
- 图片区域；
- 平台镜像和 Flavor；
- 完整拓扑一次性导入。

## 1.2 暂不实现

当前不实现：

- 多 Agent 编排；
- Web 管理后台；
- 独立 HTTP 服务；
- 多模型供应商插件体系；
- 消息队列；
- 数据库；
- 自动化测试框架；
- 复杂指标平台；
- 完整运行重放系统；
- 无限修复循环；
- 自动创建平台工程；
- 自动生成 `projectId` 或 `networkId`；
- 图片中不可见的 OSPF、静态路由、NAT、端口映射和运维描述。

---

## 2. 项目目录

```text
network-topology-agent/
├── README.md                              # 安装、配置、运行方式和操作说明。
├── pyproject.toml                         # Python 工程和依赖配置。
├── requirements.lock                     # 固定依赖版本。
├── .env.example                          # 模型密钥和平台认证变量示例。
├── task.example.yaml                     # 单次任务输入示例。
├── .gitignore                            # 排除密钥、运行产物和缓存。
│
├── config/
│   ├── app.yaml                          # 模型、平台、图片处理、默认值和调用预算。
│   └── device_mapping.yaml               # 设备语义类型到平台类型和镜像关键词的映射。
│
├── prompts/
│   ├── system.md                         # Agent 总约束和禁止事项。
│   ├── extraction.md                     # 图片拓扑结构化提取提示词。
│   └── repair.md                         # 对指定低置信度区域进行一次局部复核。
│
├── skills/
│   ├── topology_recognition.md            # 节点、文字、接口、连线和区域识别规则。
│   ├── network_reasoning.md               # 广播域、CIDR、网关和一致性规则。
│   └── platform_mapping.md                # 平台节点、网络、子网和链路映射规则。
│
├── docs/
│   ├── development_plan.md               # 本开发计划。
│   ├── architecture.md                   # 总体数据流和职责边界。
│   ├── topology_ir.md                    # Topology IR 数据结构。
│   └── platform_mapping.md               # 平台字段和接口适配规则。
│
├── src/
│   └── topology_agent/
│       ├── __init__.py                   # 包入口。
│       ├── __main__.py                   # python -m topology_agent 入口。
│       ├── cli.py                        # 读取图片、projectId、networkId 等任务参数。
│       ├── config.py                     # 加载 YAML 和环境变量。
│       ├── models.py                     # Observation、IR、Payload、验证结果等数据模型。
│       ├── image.py                      # 图片读取、缩放、切片、裁剪和坐标转换。
│       ├── llm.py                        # 多模态模型调用、结构化输出和有限重试。
│       ├── recognition.py                # 全图/局部识别并生成 TopologyObservation。
│       ├── topology.py                   # 规范化、去重、接口绑定和网络语义推理。
│       ├── platform.py                   # 登录、镜像、Flavor 和拓扑导入接口。
│       ├── compiler.py                   # ID、资源绑定、平台 Payload 编译和布局。
│       ├── validator.py                  # Schema、引用、图、IP 和平台验证。
│       ├── orchestrator.py               # 串联完整运行流程和控制状态。
│       └── artifacts.py                  # 保存中间 JSON、模型响应、日志和最终结果。
│
└── runtime/
    └── runs/                             # 按 taskId 保存每次运行产物。
```

工程规模目标：

```text
Python 文件：约 14 个
Python 代码：约 3,500～5,000 行
配置、Prompt、Skill：约 700～1,200 行
```

---

## 3. 模块划分

工程由 8 个模块组成。

| 模块 | 名称 | 前置模块 | 核心产物 |
|---|---|---|---|
| M01 | 工程基础与数据模型 | 无 | 配置对象、Observation、IR、Payload 模型 |
| M02 | 图片预处理 | M01 | `ImageBundle` |
| M03 | Prompt、Skill 与模型调用 | M01 | 结构化模型响应 |
| M04 | 拓扑视觉识别 | M02、M03 | `TopologyObservation` |
| M05 | IR 规范化与网络推理 | M04 | `ResolvedTopologyIR` |
| M06 | 平台接口与资源绑定 | M01 | token、镜像目录、Flavor、资源绑定 |
| M07 | Payload 编译与验证 | M05、M06 | 合法 `PlatformTopologyPayload` |
| M08 | 编排、提交与运行产物 | M01～M07 | `SubmissionResult` 和完整运行目录 |

固定数据流：

```text
图片 + projectId + networkId
  → M02 ImageBundle
  → M03 模型调用能力
  → M04 TopologyObservation
  → M05 ResolvedTopologyIR
  → M06 ResourceBinding
  → M07 PlatformTopologyPayload
  → M08 平台导入
```

---

# 4. M01：工程基础与数据模型

## 4.1 开发目标

完成项目骨架、配置读取和所有核心数据模型，为后续模块提供稳定接口。

## 4.2 需要实现

### 配置

配置文件采用以下结构：

```text
config/app.yaml
config/device_mapping.yaml
```

`app.yaml` 包含：

- 模型 Base URL、模型名、温度、Token、超时；
- 平台 Base URL、接口路径和成功码；
- 图片最大尺寸、切片尺寸和重叠比例；
- 默认 version、MTU、DHCP、DNS；
- 最大模型调用次数；
- 最大局部复核次数；
- 运行产物目录。

敏感信息由环境变量读取：

- 模型 API Key；
- 平台用户名；
- 平台密码。

`projectId`、`networkId` 不写入静态配置。

### 数据模型

集中定义在 `models.py`：

- `TaskInput`
- `BoundingBox`
- `Evidence`
- `ObservedNode`
- `ObservedInterface`
- `ObservedLink`
- `ObservedRegion`
- `TopologyObservation`
- `NodeIR`
- `InterfaceIR`
- `LinkIR`
- `RegionIR`
- `SegmentIR`
- `TopologyIR`
- `ResourceBinding`
- `PlatformTopologyPayload`
- `ValidationIssue`
- `ValidationReport`
- `SubmissionResult`

模型应能生成供大模型结构化输出使用的 JSON Schema，但不维护独立、重复的手写 Schema 文件。

### 基础异常

至少区分：

- 输入错误；
- 配置错误；
- 模型错误；
- 拓扑未解析；
- 平台资源错误；
- Payload 验证错误；
- 平台提交错误；
- 提交状态不确定。

## 4.3 主要文件

```text
src/topology_agent/config.py
src/topology_agent/models.py
src/topology_agent/__init__.py
config/app.yaml
config/device_mapping.yaml
```

## 4.4 代码量预估

```text
500～750 行 Python
100～180 行配置
```

## 4.5 验收标准

| 测试内容 | 测试操作 | 期望结果 |
|---|---|---|
| 合法配置 | 填写模型和平台配置并启动 | 配置成功加载，敏感值可由环境变量覆盖 |
| 缺少模型地址 | 删除模型 Base URL | 启动立即失败，并指出缺失字段 |
| 缺少 `projectId` | 构造不含 `projectId` 的任务输入 | 任务输入校验失败，不进入图片处理 |
| 缺少 `networkId` | 构造不含 `networkId` 的任务输入 | 任务输入校验失败，不进入图片处理 |
| Observation Schema | 生成模型输出 Schema | 包含节点、接口、链路、区域、证据和未解析项 |
| Payload 模型 | 读取 `111.json`、`222.json` | 两个样例均能被平台 Payload 模型解析 |
| 非法节点类型 | 将 `Node.type` 改为未支持值 | 模型校验失败并指出字段路径 |
| 坐标类型 | 将平台 `x/y` 设置为数字 | Payload 校验失败；平台坐标必须为字符串 |

M01 完成条件：

> 配置、任务输入和所有核心对象都能被强类型模型表示，后续模块不再定义重复的数据结构。

---

# 5. M02：图片预处理

## 5.1 开发目标

将输入图片转换为多模态模型可读取的全局图和必要切片，并保证坐标可以映射回原图。

## 5.2 需要实现

在 `image.py` 中实现：

- PNG/JPG/JPEG 加载；
- EXIF 方向纠正；
- RGB 转换；
- 原图哈希；
- 全局缩放图；
- 大图重叠切片；
- 指定区域裁剪；
- 切片坐标到原图坐标转换；
- 原图坐标到平台坐标的基础转换函数。

当前不实现：

- 复杂 OpenCV 线条检测；
- 多种增强算法自动选择；
- OCR 专用流水线；
- 图像缓存数据库。

图片增强只保留一次保守锐化或对比度调整，避免破坏连线。

## 5.3 主要文件

```text
src/topology_agent/image.py
```

## 5.4 代码量预估

```text
300～450 行 Python
```

## 5.5 验收标准

| 测试内容 | 测试操作 | 期望结果 |
|---|---|---|
| 样例图片加载 | 分别输入 `111.png`、`222_v1.png` | 两张图片均成功读取，宽高和格式正确 |
| JPG 支持 | 将一张样例转换为 JPG 后输入 | 正常生成全局图和切片 |
| 损坏图片 | 输入不可解析的伪 PNG | 明确返回图片输入错误 |
| 大图切片 | 使用 `222_v1.png` 生成重叠切片 | 原图所有区域均被覆盖，无空白区 |
| 坐标还原 | 选择切片中一个点映射回原图 | 误差不超过 1 像素 |
| 宽高比 | 比较缩放前后尺寸 | 宽高比误差小于 0.1% |
| 线条保持 | 人工对比原图和增强图 | 主要连线未被删除、粘连或新增 |
| 哈希用途 | 检查代码逻辑 | 哈希只用于运行记录，不用于选择预存答案 |

M02 完成条件：

> 能稳定产生全图、切片和坐标映射，不需要后续模块了解图片处理细节。

---

# 6. M03：Prompt、Skill 与模型调用

## 6.1 开发目标

完成三个 Skill 的加载、Prompt 组装和多模态模型结构化调用。

## 6.2 需要实现

### Prompt

保留三个 Prompt：

- `system.md`
- `extraction.md`
- `repair.md`

不单独保留完整 audit Prompt。初次识别结果的自审要求写入 `extraction.md`。

### Skill

保留三个 Skill：

- `topology_recognition.md`
- `network_reasoning.md`
- `platform_mapping.md`

模型调用时：

- 图片识别加载 `topology_recognition`；
- 需要模型辅助判断网络歧义时加载 `network_reasoning`；
- 平台语义核对时加载 `platform_mapping`；
- 默认不一次发送三个完整 Skill。

### 模型客户端

在 `llm.py` 中实现一个 OpenAI 兼容客户端，支持：

- 图片和文本输入；
- JSON Schema 结构化输出；
- 普通文本中的 JSON 提取；
- 超时；
- 429、502、503、504 有限重试；
- 调用次数和 Token 记录；
- API Key 脱敏。

不实现多供应商插件层。通过 Base URL 和模型名切换 OpenAI 兼容服务。

## 6.3 主要文件

```text
src/topology_agent/llm.py
prompts/system.md
prompts/extraction.md
prompts/repair.md
skills/topology_recognition.md
skills/network_reasoning.md
skills/platform_mapping.md
```

## 6.4 代码量预估

```text
350～500 行 Python
450～700 行 Prompt/Skill
```

## 6.5 验收标准

| 测试内容 | 测试操作 | 期望结果 |
|---|---|---|
| 多模态调用 | 发送一张样例图和 Observation Schema | 模型返回可解析的结构化 JSON |
| JSON 代码块 | 让模拟响应返回 Markdown JSON 代码块 | 能正确提取并解析 |
| 非 JSON 响应 | 返回普通自然语言 | 返回结构化模型错误，不继续运行 |
| 缺失字段 | 返回缺少 `observedNodes` 的 JSON | Schema 校验失败并指出字段 |
| 429 | 模拟一次 429 后成功 | 按策略重试并成功返回 |
| 400 | 模拟请求参数错误 | 不进行无意义重试 |
| 超出预算 | 达到最大模型调用次数 | 停止调用并将任务标记为模型失败 |
| Skill 加载 | 执行图片识别请求 | 只加载 `topology_recognition`，不加载另外两个完整 Skill |
| 固定答案检查 | 搜索 Prompt 和 Skill | 不存在样例完整 JSON、固定工程 ID 或固定镜像 ID |

M03 完成条件：

> 上层只需提供图片、任务描述和目标模型，即可得到经过 Schema 校验的结构化响应。

---

# 7. M04：拓扑视觉识别

## 7.1 开发目标

使用全图和有限局部裁剪，从图片生成 `TopologyObservation`。

## 7.2 需要实现

在 `recognition.py` 中完成：

1. 全图识别：
   - 区域；
   - 节点候选；
   - 节点名称；
   - 粗粒度设备类型；
   - 主要链路；
   - 接口和 IP 文本。
2. 对低置信度或小文字区域进行局部裁剪复核。
3. 合并全图和局部结果。
4. 为节点、接口、IP、链路和区域保存：
   - 原始文本；
   - 边界框；
   - 置信度；
   - 证据；
   - 候选值。
5. 将无法可靠判断的内容放入 `unresolvedItems`。

调用策略：

```text
全图识别：1 次
局部补充：最多 2 批
```

不实现：

- 独立 overview/node/link/region 多级 Agent；
- 大量逐节点模型调用；
- 无限视觉复核。

## 7.3 主要文件

```text
src/topology_agent/recognition.py
```

## 7.4 代码量预估

```text
400～600 行 Python
```

## 7.5 验收标准

### 111

| 检查项 | 期望结果 |
|---|---:|
| 节点数 | 9 |
| 二层交换机 | 3 |
| 路由器 | 2 |
| 客户端 | 4 |
| 链路数 | 8 |
| 区域数 | 0 |

额外要求：

- 不把 IP、接口文字或普通标签识别为节点；
- 每个节点具有名称、类型、边界框、置信度和证据；
- 每条链路具有两个存在的节点候选；
- 模糊文字进入候选或未解析项。

### 222

| 检查项 | 期望结果 |
|---|---:|
| 节点数 | 32 |
| 二层交换机 | 10 |
| 路由器 | 3 |
| 防火墙 | 1 |
| 客户端 | 12 |
| 服务器类节点 | 6 |
| 链路数 | 31 |
| 区域数 | 4 |

区域名称：

- `LAB JARINGAN`
- `RUANG SERVER`
- `MANAJEMENT`
- `CLIENT`

额外要求：

- 区域框不被识别为链路；
- 区域不被识别为节点；
- 不根据防火墙图标自动生成 NAT 或端口映射；
- 图片中不可见的描述和 OSPF 不进入 Observation。

M04 完成条件：

> Observation 能准确表达图片中可见的节点、文字、连线和区域，并显式保存不确定性。

---

# 8. M05：IR 规范化与网络推理

## 8.1 开发目标

将 Observation 转换为稳定的 `ResolvedTopologyIR`，完成节点去重、接口绑定、广播域、CIDR 和网关推理。

## 8.2 需要实现

在 `topology.py` 中实现：

### 规范化

- 重叠切片节点去重；
- `rawName` 和 `normalizedName`；
- 接口与节点绑定；
- IP 与接口绑定；
- 无向链路去重；
- 区域成员归属；
- 临时 ID 分配；
- 未解析项整理。

### 网络推理

- 每个二层交换机建立一个广播域；
- 找出广播域成员设备和接口；
- 根据 IP/前缀计算 CIDR；
- 选择路由器、防火墙或三层交换机接口作为网关；
- 检查重复 IP；
- 检查接口重复连接；
- 检查 IP 是否属于网段；
- 检查网关是否属于网段；
- 处理路由器直连等点到点链路。

不允许：

- 只看到裸 IP 就默认 `/24`；
- 多个网关候选无法消解时随机选择；
- 用默认值覆盖明确视觉证据。

只有出现少量视觉歧义时，才允许通过 M03 进行一次局部复核。

## 8.3 主要文件

```text
src/topology_agent/topology.py
```

## 8.4 代码量预估

```text
550～800 行 Python
```

## 8.5 验收标准

### 通用

| 测试内容 | 测试操作 | 期望结果 |
|---|---|---|
| 重叠节点 | 将同一节点放入两个切片结果 | 只保留一个 NodeIR |
| 同名不同节点 | 构造两个同名、位置不同的节点 | 不被错误合并 |
| 反向重复链路 | 同时提供 A→B、B→A | 只保留一条 LinkIR |
| 接口归属 | 查看接口引用 | 每个接口恰好属于一个节点 |
| 裸 IP | 输入无前缀 `10.0.0.1` 且无其他证据 | 生成 `UNKNOWN_PREFIX`，阻止编译 |
| 重复 IP | 同一广播域输入重复地址 | 返回重复 IP 错误 |
| 错误网关 | 网关不属于子网 | 返回网关错误 |
| 多网关候选 | 提供两个无法区分的候选 | 状态为拓扑未解析，不随机选择 |

### 111

期望：

- 9 个 NodeIR；
- 8 个 LinkIR；
- 3 个 SegmentIR；
- 子网：
  - `192.168.1.0/24`
  - `192.168.2.0/24`
  - `192.168.12.0/24`
- 网关：
  - `192.168.1.1`
  - `192.168.2.1`
  - `192.168.12.1`
- 所有 NIC IP 属于对应网段；
- 不存在阻塞性未解析项。

### 222

期望：

- 32 个 NodeIR；
- 31 个 LinkIR；
- 10 个 SegmentIR；
- 10 个子网；
- 4 个 RegionIR；
- 所有路由器和防火墙接口只属于一个正确子网；
- 同一子网无重复 IP；
- 不存在阻塞性未解析项。

M05 完成条件：

> IR 已经能够直接作为平台资源匹配和 Payload 编译的输入，无需人工修改。

---

# 9. M06：平台接口与资源绑定

## 9.1 开发目标

实现平台认证、镜像查询、Flavor 查询、资源绑定和标准拓扑导入接口基础调用。

## 9.2 需要实现

在 `platform.py` 中实现：

### 登录

```text
POST /identity/anonymity/unsafe/login
```

提取 token，并放入：

```text
authorization: <token>
```

### 镜像查询

```text
POST /image/list
```

要求：

- 支持分页；
- 读取到 `data.total`；
- 只使用可用镜像；
- 保存真实 `imageId`、名称、系统、架构、`nodeType`、最低资源。

### Flavor 查询

```text
POST /flavor/listBySpace
```

要求：

- 获取可用 CPU、RAM、Disk；
- 满足镜像最低要求；
- 选择浪费较小的合法配置。

### 资源绑定

根据 `semanticType` 和 `device_mapping.yaml`：

- client → PC 镜像；
- router → 路由器镜像；
- firewall → 防火墙镜像；
- server 类 → 对应服务器镜像；
- SW 不绑定普通 VM 镜像。

每个 VM 保存：

- `imageId`
- `imageName`
- `sysType`
- Flavor
- 选择理由。

### 导入接口

预留：

```text
POST /projects/meshTopo/agent/import
```

实际提交由 M08 调用。

本模块不创建工程，也不修改外部 `projectId`、`networkId`。

## 9.3 主要文件

```text
src/topology_agent/platform.py
```

## 9.4 代码量预估

```text
450～650 行 Python
```

## 9.5 验收标准

| 测试内容 | 测试操作 | 期望结果 |
|---|---|---|
| 登录成功 | 使用合法账号调用登录接口 | 正确提取 token |
| 登录失败 | 使用错误账号 | 返回平台认证错误，不查询资源 |
| 成功码兼容 | 分别返回配置允许的 `code=1/200` | 均可根据完整响应正确判断 |
| 镜像分页 | 镜像数量大于 pageSize | 查询到 `total` 对应的全部镜像 |
| token 失效 | 资源请求返回认证失效 | 重新登录一次并重试 |
| 无路由镜像 | 删除所有路由器候选 | 资源绑定失败，不生成占位 imageId |
| Flavor 不足 | 所有 Flavor 小于镜像最低要求 | 资源绑定失败 |
| SW 资源 | 输入二层交换机 | 不绑定普通 VM 镜像 |
| 外部 ID | 查看平台模块输入输出 | `projectId/networkId` 原样传递，未被生成或改写 |

M06 完成条件：

> 每个 VM 节点都能绑定当前平台中真实存在的镜像和合法 Flavor。

---

# 10. M07：Payload 编译与验证

## 10.1 开发目标

将 `ResolvedTopologyIR` 和资源绑定结果编译为完整平台 JSON，并在提交前完成强制验证。

## 10.2 需要实现

### 编译

在 `compiler.py` 中实现：

- 外部 `projectId/networkId` 写入；
- 内部 ID 注册表；
- 节点编译；
- NIC 编译；
- Link 编译；
- Network 编译；
- Subnet 编译；
- 区域属性编译；
- 图片坐标到平台坐标转换；
- `sample_v2` 兼容字段；
- 默认 `version = v2`；
- 无明确输入时 `portMappingList = []`。

内部 ID 建议：

| 对象 | 前缀 |
|---|---|
| VM | V |
| SW | W |
| TSW | T |
| NIC | P |
| Link | L |
| Network | G |
| Subnet | S |

编译规则：

- client → `VM/CLIENT`
- server 类 → `VM/SERVER`
- router → `VM/DRT`
- firewall → `VM/FW`
- switch_l2 → `SW/SW`
- switch_l3 → `TSW/TSW`
- 每个二层广播域生成一个 Network 和一个 Subnet；
- `Network.nodeId` 指向交换机；
- `Network.transmitNodeIdList` 指向广播域其他节点；
- `NIC.subnetId` 指向对应 Subnet；
- 交换机连接 VM 时，交换机作为 `sDevId`。

### 验证

在 `validator.py` 中实现五类检查：

1. 字段和类型；
2. ID 和引用；
3. 图结构；
4. IP、CIDR 和网关；
5. 平台类型、镜像和 Flavor。

自动修复仅保留：

- 字符串类型转换；
- 空数组补全；
- 重复内部 ID 重建；
- 链路方向统一；
- 完全重复链路删除；
- 根据已知 IP/前缀重新计算 CIDR。

视觉事实不在此模块修改。仍有错误时停止提交。

## 10.3 主要文件

```text
src/topology_agent/compiler.py
src/topology_agent/validator.py
```

## 10.4 代码量预估

```text
700～1,000 行 Python
```

## 10.5 验收标准

### 通用错误拦截

| 测试内容 | 测试操作 | 期望结果 |
|---|---|---|
| 重复 ID | 将两个节点设置为同一 ID | 验证失败并指出重复 ID |
| 无效 Link | Link 引用不存在节点 | 验证失败并指出 Link 和缺失节点 |
| 无效 subnetId | NIC 引用不存在子网 | 验证失败 |
| 错误 CIDR | NIC IP 不属于对应 CIDR | 验证失败 |
| 错误 gateway | 网关不属于子网 | 验证失败 |
| 虚构 imageId | VM 使用资源目录不存在的 imageId | 验证失败 |
| 类型冲突 | SW 使用 `VM/CLIENT` 属性 | 验证失败 |
| 外部 ID | 比较任务输入和 Payload 根字段 | `projectId/networkId` 逐字符一致 |
| 提交门禁 | Payload 存在任一 ERROR | 不允许进入 M08 平台提交 |

### 111 最终 Payload

| 检查项 | 期望结果 |
|---|---:|
| Node | 9 |
| Link | 8 |
| Network | 3 |
| Subnet | 3 |
| Port Mapping | 0 |
| `SW/SW` | 3 |
| `VM/DRT` | 2 |
| `VM/CLIENT` | 4 |

额外要求：

- 每个交换机 `nicList=[]`；
- 所有 VM NIC 引用存在的 Subnet；
- 不复制参考 JSON 中节点名称与 NIC 名称的不一致；
- 通过全部验证。

### 222 最终 Payload

| 检查项 | 期望结果 |
|---|---:|
| Node | 32 |
| Link | 31 |
| Network | 10 |
| Subnet | 10 |
| `SW/SW` | 10 |
| `VM/DRT` | 3 |
| `VM/FW` | 1 |
| `VM/CLIENT` | 12 |
| `VM/SERVER` | 6 |

额外要求：

- 四个区域通过 `district/fillColor` 写入成员节点；
- 不生成区域节点；
- 纯图片任务 `portMappingList=[]`；
- 不复制图片不可见的 OSPF、描述和运维属性；
- 通过全部验证。

M07 完成条件：

> 可以稳定生成不需要人工编辑、并且能够进入平台导入阶段的完整 Payload。

---

# 11. M08：编排、提交与运行产物

## 11.1 开发目标

通过一条命令串联完整运行流程，保存必要产物，并安全调用标准拓扑导入接口。

## 11.2 需要实现

### CLI

示例：

```bash
python -m topology_agent \
  --image ./topology.png \
  --project-id <平台界面生成的ID> \
  --network-id <平台界面生成的ID>
```

认证和模型密钥从环境变量读取。

### 编排流程

```text
1. 校验任务输入
2. 图片预处理
3. 登录平台并读取资源
4. 调用模型识别拓扑
5. 生成和解析 IR
6. 绑定镜像和 Flavor
7. 编译 Payload
8. 执行验证
9. 通过后提交平台
10. 保存结果
```

### 简化状态

状态设计仅保留：

```text
CREATED
RUNNING
VALIDATION_FAILED
SUBMISSION_UNCERTAIN
FAILED
COMPLETED
```

具体失败原因放入错误对象，不再为每个模块建立独立状态枚举。

### 提交保护

- 提交前保存 `payload.json`；
- 计算 Payload Hash；
- 明确成功后记录完成；
- 写入超时且无法确认结果时，状态为 `SUBMISSION_UNCERTAIN`；
- 该状态下不自动重复提交；
- token 失效只允许重新登录并重试一次。

### 运行产物

```text
runtime/runs/<taskId>/
├── input.png
├── model_response.json
├── observation.json
├── resolved_topology_ir.json
├── resource_binding.json
├── payload.json
├── validation_report.json
├── submission_result.json
└── run.log
```

不实现复杂 replay、metrics 和 trace 子系统。

## 11.3 主要文件

```text
src/topology_agent/cli.py
src/topology_agent/orchestrator.py
src/topology_agent/artifacts.py
src/topology_agent/__main__.py
```

## 11.4 代码量预估

```text
450～650 行 Python
```

## 11.5 验收标准

| 测试内容 | 测试操作 | 期望结果 |
|---|---|---|
| 完整 CLI | 提供图片和合法外部 ID | 一条命令完成识别、编译、验证和提交 |
| 缺少图片 | 不传 `--image` | CLI 明确报错 |
| 缺少外部 ID | 缺少任一 ID | 不启动模型调用 |
| 模型失败 | 模拟模型不可用 | 状态为 FAILED，不调用导入接口 |
| 拓扑未解析 | 保留阻塞性未解析项 | 状态为 FAILED，不编译或不提交 |
| 验证失败 | 构造非法 Payload | 状态为 VALIDATION_FAILED，不提交 |
| 成功提交 | 平台明确返回成功 | 状态为 COMPLETED，保存全部产物 |
| 提交超时 | 平台写请求超时 | 状态为 SUBMISSION_UNCERTAIN，不自动重复提交 |
| 密钥保护 | 检查 `run.log` | 不包含 API Key、密码和完整 token |
| 运行目录 | 检查 taskId 目录 | 9 类规定产物均存在 |

### 111 端到端

期望：

- Payload 统计符合 M07；
- 平台返回成功；
- 平台绘制 9 个节点和 8 条链路；
- 相对布局与原图一致；
- 不需要人工修改 JSON。

### 222 端到端

期望：

- Payload 统计符合 M07；
- 平台返回成功；
- 平台绘制 32 个节点和 31 条链路；
- 四个区域正确体现；
- 不需要人工修改 JSON。

M08 完成条件：

> 操作人员在平台界面创建工程并取得两个外部 ID 后，可以通过一条命令完成整套拓扑导入。

---

## 12. 开发顺序

| 顺序 | 模块 | 开始条件 | 完成门禁 |
|---|---|---|---|
| 1 | M01 | 无 | 配置和数据模型可用 |
| 2 | M02 | M01 | 图片和坐标处理稳定 |
| 3 | M03 | M01 | 模型能返回结构化结果 |
| 4 | M04 | M02、M03 | 111/222 Observation 统计正确 |
| 5 | M05 | M04 | 111/222 IR 和子网正确 |
| 6 | M06 | M01；可与 M02～M05 并行开发 | 真实镜像和 Flavor 可绑定 |
| 7 | M07 | M05、M06 | 最终 Payload 通过全部验证 |
| 8 | M08 | M01～M07 | 一条命令完成平台导入 |

建议并行关系：

```text
M01
├── M02 → M04 → M05 ─┐
├── M03 ──────────────┤
└── M06 ──────────────┤
                      ↓
                     M07
                      ↓
                     M08
```

---

## 13. 代码量控制

| 模块 | 预计 Python 行数 |
|---|---:|
| M01 | 500～750 |
| M02 | 300～450 |
| M03 | 350～500 |
| M04 | 400～600 |
| M05 | 550～800 |
| M06 | 450～650 |
| M07 | 700～1,000 |
| M08 | 450～650 |
| **合计** | **3,700～5,400** |

工程控制规则：

- 普通 Python 文件控制在 150～450 行；
- `models.py` 可放宽到 700 行；
- 不为一个简单类单独创建文件；
- 不创建抽象基类，除非至少有两个实际实现；
- 不实现未来可能使用但当前不需要的扩展点；
- 不创建测试目录和测试框架；
- 不在代码中保留 TODO 占位模块；
- 每个模块完成后立即按验收标准手动检查。

---

## 14. 实现约束与取舍

| 不采用的设计 | 当前处理 |
|---|---|
| 过多模块拆分 | 采用 8 个核心模块 |
| 过细文件拆分 | 控制为约 14 个 Python 文件 |
| 多模型 Provider 抽象 | 使用 OpenAI 兼容 API |
| 多层 Recognition 子模块 | 使用一个 `recognition.py` |
| 多个规范化文件 | 使用一个 `topology.py` |
| 多个编译器文件 | 使用一个 `compiler.py` |
| 多个验证器文件 | 使用一个 `validator.py` |
| 独立 repair 包 | 保留一次局部复核和少量确定性修复 |
| 复杂状态机 | 使用 6 个核心状态 |
| metrics/trace/replay | 不纳入当前实现 |
| HTTP service | 不纳入当前实现 |
| 独立手写 JSON Schema | 由数据模型生成 |
| strict_document/sample_v2 双模式 | 使用已验证的 `sample_v2` |
| 复杂端口映射支持 | 默认空，仅明确输入时处理 |
| 自动测试框架 | 不建立，直接使用模块验收标准 |

---

## 15. 最终交付标准

工程完成时必须满足：

- 一条命令完成图片到平台导入；
- 只有一个逻辑 Agent；
- 恰好三个 Skill；
- `projectId/networkId` 只从任务输入读取；
- 111 和 222 的节点、链路、设备类型、网络和子网统计正确；
- 所有 VM 使用平台实时返回的真实镜像和合法 Flavor；
- 最终 Payload 通过字段、引用、图、IP 和平台验证；
- 纯图片任务不生成图片不可见配置；
- 验证失败时不提交；
- 写入超时不自动重复提交；
- 日志不泄露密钥；
- Python 代码量控制在约 3,700～5,400 行；
- 不包含 `tests/`、数据库、队列、服务后台和无用扩展层。

最终实现原则：

> 保留影响识别正确率和导入成功率的功能，删除主要服务于长期维护、多人协作和未来扩展的工程层。
