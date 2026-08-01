# 网络拓扑识别 Agent 架构设计

## 1. 文档目的

本文定义网络拓扑识别 Agent 的总体架构、模块边界、数据流、运行状态和可靠性约束。

系统接收：

- 一张 PNG、JPG 或 JPEG 网络拓扑图片；
- 操作人员在平台界面创建工程后取得的 `projectId`；
- 操作人员在平台界面创建工程后取得的 `networkId`；
- 多模态模型 API 配置；
- 平台地址和认证信息。

系统输出：

- 图片识别结果；
- 平台无关的 Topology IR；
- 完成网络推理后的 Resolved Topology IR；
- 平台资源绑定结果；
- 完整平台拓扑 JSON；
- 验证报告；
- 平台导入结果。

核心目标是形成稳定的单向链路：

```text
拓扑图片
  → structure 完整全图视觉识别
  → links 编号标注完整全图视觉识别
  → text 文字增强完整全图视觉识别
  → 程序预融合
  → fusion 纯文本语义融合补丁
  → 程序最终融合和 Observation 强校验
  → 网络语义恢复
  → 平台对象编译
  → 提交前验证
  → 完整拓扑导入
```

---

## 2. 设计原则

### 2.1 单 Agent

系统只包含一个逻辑 Agent。

Agent 负责：

- 使用结构、编号标注链路和文字增强三个完整全图视图；
- 分阶段识别节点、区域、链路、文字、接口和 IP 候选；
- 对三阶段结构化观察进行一次纯文本语义融合；
- 输出符合各阶段强类型 Schema 的视觉观察或融合补丁。

Agent 不负责：

- 生成平台对象 ID；
- 生成 `projectId` 或 `networkId`；
- 查询或编造平台镜像 ID；
- 自由设计图片中不存在的网络配置；
- 绕过验证直接调用导入接口；
- 无限循环修复。

### 2.2 三个 Skill

系统保留三个领域 Skill：

| Skill | 作用 |
|---|---|
| `topology_recognition.md` | 设备图标、节点名称、接口、IP、连线和区域识别 |
| `network_reasoning.md` | 广播域、CIDR、网关、重复 IP 和接口一致性 |
| `platform_mapping.md` | 语义设备类型到平台对象、字段和资源角色的映射 |

Skill 只保存领域规则，不保存：

- 完整样例 JSON；
- 固定 `projectId`；
- 固定 `networkId`；
- 固定 `imageId`；
- token；
- 图片哈希到答案的映射。

### 2.3 大模型与程序分工

```text
视觉事实识别             → Qwen3.7-Plus 三次全图视觉调用
已有候选的语义裁决       → Qwen3.7-Plus 一次纯文本融合调用
稳定编号和确定性预融合   → 程序
融合补丁校验和最终应用   → 程序
Observation 强校验       → 程序
IR 去重和网络语义推理    → 程序（M05）
平台资源查询             → 程序
内部对象 ID              → 程序
平台 JSON 编译           → 程序
提交前验证               → 程序
完整拓扑导入             → 程序
```

原则：

> 大模型处理需要视觉理解的内容，程序处理可计算、可枚举和可验证的内容。

---

## 3. 工程结构

```text
network-topology-agent/
├── config/
│   ├── app.yaml
│   └── device_mapping.yaml
├── prompts/
│   ├── system.md
│   ├── extraction.md
│   └── repair.md
├── skills/
│   ├── topology_recognition.md
│   ├── network_reasoning.md
│   └── platform_mapping.md
├── docs/
│   ├── development_plan.md
│   ├── architecture.md
│   ├── topology_ir.md
│   └── platform_mapping.md
├── src/topology_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── models.py
│   ├── image.py
│   ├── llm.py
│   ├── recognition.py
│   ├── topology.py
│   ├── platform.py
│   ├── compiler.py
│   ├── validator.py
│   ├── orchestrator.py
│   └── artifacts.py
└── runtime/runs/
```

不建立：

- `tests/`；
- `manual_tests/`；
- 数据库目录；
- 消息队列；
- Web 服务目录；
- 多供应商插件目录。

---

## 4. 八个模块

| 模块 | 名称 | 对应文件 | 主要职责 |
|---|---|---|---|
| M01 | 工程基础与数据模型 | `config.py`、`models.py` | 配置、任务输入、Observation、IR、Payload、错误模型 |
| M02 | 图片预处理 | `image.py` | 图片加载、完整全图缩放、文字增强、编号标注、坐标转换 |
| M03 | Prompt、Skill 与模型调用 | `llm.py`、`prompts/`、`skills/` | 多模态调用、结构化输出、有限重试 |
| M04 | 拓扑视觉识别 | `recognition.py` | 三次全图识别、一次文本融合和 Observation 强校验 |
| M05 | IR 规范化与网络推理 | `topology.py` | 去重、接口绑定、链路恢复、广播域、CIDR、网关 |
| M06 | 平台接口与资源绑定 | `platform.py` | 登录、镜像、Flavor、资源选择和导入接口 |
| M07 | Payload 编译与验证 | `compiler.py`、`validator.py` | 平台对象编译、引用检查和提交门禁 |
| M08 | 编排、提交与运行产物 | `orchestrator.py`、`artifacts.py`、`cli.py` | 全流程串联、状态、日志、产物和最终提交 |

---

## 5. 模块依赖

```text
M01
├── M02 ─────┐
├── M03 ─────┼──→ M04 → M05 ─┐
└── M06 ──────────────────────┼──→ M07 → M08
                              │
                              └───────────────
```

准确依赖：

| 模块 | 开始前必须完成 |
|---|---|
| M01 | 无 |
| M02 | M01 |
| M03 | M01 |
| M04 | M02、M03 |
| M05 | M04 |
| M06 | M01 |
| M07 | M05、M06 |
| M08 | M01～M07 |

M06 的登录、资源查询和响应解析可以与 M02～M05 并行开发。

---

## 6. 输入模型

任务输入 `TaskInput` 至少包含：

```text
imagePath
projectId
networkId
taskId
```

外部配置还包括：

- 模型 Base URL；
- 模型名称；
- API Key；
- 平台 Base URL；
- 平台用户名；
- 平台密码；
- 超时；
- 最大调用次数；
- 图片处理参数；
- 平台默认值。

约束：

- `projectId` 和 `networkId` 属于单次任务输入；
- 静态配置中不得写死这两个 ID；
- Agent 不创建工程；
- 编译器原样写入这两个 ID；
- 提交前验证任务输入与 Payload 根字段一致。

---

## 7. 核心数据流

```text
TaskInput
  ↓
ImageBundle
  ↓
TopologyObservation
  ↓
TopologyIR
  ↓
ResolvedTopologyIR
  ↓
ResourceBinding
  ↓
PlatformTopologyPayload
  ↓
ValidationReport
  ↓
SubmissionResult
```

### 7.1 ImageBundle

包含：

- EXIF 纠正并 RGB 规范化的原图对象和原图信息；
- 等比例缩小且覆盖完整拓扑的 `global_structure`；
- 只增强一次且覆盖完整拓扑的 `global_text`；
- Pass 1 后动态登记的编号标注完整图 `global_links`；
- 三个业务全图视图和原图之间的通用坐标映射；
- 图片 SHA-256。

图片哈希只用于运行记录，不用于答案选择。

### 7.2 TopologyObservation

保存模型直接观察结果：

- 节点候选；
- 接口和 IP 候选；
- 链路候选；
- 区域候选；
- 置信度；
- 图片证据；
- 未解析项。

此层允许重复和歧义。

### 7.3 TopologyIR

保存规范化拓扑：

- 唯一节点；
- 唯一接口；
- 唯一链路；
- 区域成员；
- 稳定临时 ID；
- 规范化名称；
- 未解析项。

此层不包含平台资源和平台对象 ID。

### 7.4 ResolvedTopologyIR

增加：

- 广播域；
- 子网 CIDR；
- 网关；
- 成员接口；
- 接口所属网段；
- 网络一致性结果。

存在阻塞性未解析项时，不进入编译阶段。

### 7.5 ResourceBinding

每个 VM 节点绑定：

- 真实 `imageId`；
- `imageName`；
- `sysType`；
- CPU；
- RAM；
- Disk；
- 匹配依据。

资源必须来自当前平台查询结果。

### 7.6 PlatformTopologyPayload

根字段：

```text
projectId
networkId
version
networkList
subnetList
nodeList
linkList
portMappingList
```

### 7.7 ValidationReport

包含：

- ERROR；
- WARNING；
- 对象路径；
- 错误码；
- 错误说明；
- 可执行修复；
- 最终是否允许提交。

### 7.8 SubmissionResult

包含：

- 平台 HTTP 状态；
- 业务码；
- 响应消息；
- Payload Hash；
- 最终任务状态；
- 是否明确写入成功。

---

## 8. 图片处理流程

M02 执行：

```text
读取图片
  → EXIF 方向纠正
  → RGB 转换
  → 保留原图对象和 SHA-256
  → 按最长边限制等比例生成 global_structure
  → 从结构全图只增强一次生成 global_text
  → Pass 1 后按稳定编号生成 global_links
  → 登记完整视图和通用坐标映射
```

要求：

- 三个业务视图都覆盖完整拓扑、保持视觉方向和宽高比；
- 原图不超过最长边限制时不放大，超过时使用高质量重采样等比例缩小；
- 不删除细线；
- 不把区域边框处理成拓扑链路；
- `global_links` 的节点和区域标注确定且不大面积遮挡连接线和文字；
- `global_text` 只进行一次轻微锐化或对比度增强；
- 任何业务视图中的点、边界框和折线都能映射回原图；
- 不生成切片、裁剪或其他局部视图，不写临时图片文件。

不依赖复杂的传统视觉算法来决定拓扑结构。

---

## 9. 模型调用流程

M03 提供统一函数，例如：

```text
call_structured(prompt, images, response_model)
```

调用规则：

1. 系统提示词加载通用约束；
2. M04 的三个视觉阶段和文本融合阶段只加载 `topology_recognition`；
3. 需要网络歧义辅助时加载 `network_reasoning`；
4. 平台语义核对时加载 `platform_mapping`；
5. 响应必须通过数据模型解析；
6. 429、502、503、504 可有限重试；
7. 参数错误、认证错误和非法响应不进行无意义重试。

活动模型代码由配置提供，当前为 `qwen3.7-plus`；沿用现有可工作的 OpenAI 兼容
Base URL 和认证，不建立供应商或模型专用客户端。M04 调用预算固定为：

```text
structure：1 次视觉逻辑调用
links：1 次视觉逻辑调用
text：1 次视觉逻辑调用
fusion：1 次纯文本逻辑调用
总计：4 次逻辑调用
```

M04 在第一次视觉请求之前检查剩余逻辑预算至少为 4。逻辑调用和真实 HTTP 请求分开
计数；有限传输重试或现有 JSON/Schema 修复可能使 HTTP 请求数大于 4，但不构成额外
业务阶段。任何阶段失败都不得跳过后续步骤后返回半成品 Observation。

---

## 10. 视觉识别与融合流程

M04 严格串行执行：

```text
global_structure
  → Pass 1 structure：节点、区域和粗粒度类型候选
  → 程序稳定分配 Nxxx/Rxxx 并生成 global_links
  → Pass 2 links：链路、端点候选、折线和新增节点候选
global_text
  → Pass 3 text：名称、接口、IP、前缀和文字归属候选
  → 程序转换原图坐标、稳定分配 ID、合并完全相同项并建立冲突
  → Pass 4 fusion：仅对已有 conflictId/candidateIndex 输出文本融合补丁
  → 程序校验并应用补丁、重新计算 summary、执行完整一致性检查
  → TopologyObservation
```

Pass 1 只观察结构事实；Pass 2 只观察线条和端点；Pass 3 只观察可见文字及其空间
关系。每个视觉阶段都保留原始文字、候选值、置信度、当前完整视图坐标和 Evidence，
不确定内容进入候选或未解析项。

程序预融合负责把三个完整视图坐标统一转换为 EXIF 纠正后的原图像素坐标，分配稳定
Observation、Evidence、未解析项和冲突 ID，并验证明确引用。重复节点、类型、名称、
文字归属、接口归属、IP 归属、链路端点、区域成员、交叉线和 Evidence 冲突等语义
歧义不得仅依靠最近距离或最高视觉置信度强制消解。

第四次 Fusion 调用不发送任何图片，只接收语义融合所需的紧凑结构化上下文。模型只能
引用输入中的 `conflictId` 和 `candidateIndex`，输出选择、保留、合并、分离、绑定或保持
未解析的强类型补丁；不能创建对象、修改稳定 ID、坐标、折线、Evidence 或来源视图。
即使没有冲突也执行第四次调用并返回空决策。

M04 为每次执行创建 `runtime/runs/<taskId>/attempt_###/`，并在其中的
`recognition.jsonl` 记录 structure、links、text、fusion 各阶段的开始、成功或失败、真实
HTTP/Token 统计和脱敏错误类别。三个视觉阶段实际使用的完整视图分别保存为
`global_structure.png`、`global_links.png` 和 `global_text.png`，作为 M04 运行验收
产物而非 M02 临时文件；Fusion 仅保存纯文本上下文和结构化补丁，不生成或发送第四张图片。

每次请求发送前保存 `*_context.json`，仅含 task、全图坐标关系和程序构造的紧凑结构化摘要；
视觉阶段成功后保存对应的归一化 Evidence 快照，最终通过强校验后保存
`topology_observation.json`。这些文件用于本地上下文管理、审计和失败定位，不能含密钥、图片
data URL、完整 Prompt、Schema、原始模型响应或 reasoning_content，且绝不作为响应缓存跳过
固定四阶段调用。重跑始终创建新 attempt 并重新发起四次业务调用。

程序不会无条件信任融合补丁。高置信度唯一裁决仍保留原始候选和 Evidence；中等置信度
只调整排序；低置信度保持歧义。未知冲突、非法候选索引、非法引用或对象类型不匹配的
决策被拒绝并形成或保留未解析项，不发起第五次模型调用。

最终校验覆盖原图几何范围、正面积边界框、中心、折线、稳定 ID 唯一性、对象引用、
Evidence 引用、来源视图和 Pydantic Schema。Evidence 的 `sourceViewId` 只允许：

```text
global_structure
global_links
global_text
```

`summary` 由程序根据最终列表重新计算，不能采用任一模型阶段给出的统计。

模型不得自动生成：

- OSPF；
- 静态路由；
- NAT；
- 端口映射；
- 运维描述；
- 图片中不可见的系统配置。

CIDR、网关、广播域和其他网络语义恢复继续由 M05 负责；M04 不生成 TopologyIR 或平台
Payload。

---

## 11. 拓扑恢复流程

M05 包含两部分。

### 11.1 规范化

程序完成：

- 重叠节点去重；
- 同名不同节点区分；
- 接口与节点绑定；
- IP 与接口绑定；
- 无向链路去重；
- 区域成员确定；
- 临时 ID 生成；
- 未解析项整理。

### 11.2 网络推理

程序完成：

- 每个二层交换机建立一个广播域；
- 找出成员节点和成员接口；
- 根据 IP 和前缀计算 CIDR；
- 选择路由器、防火墙或三层交换机接口作为网关；
- 检查重复 IP；
- 检查网关合法性；
- 检查接口是否被重复使用；
- 处理点到点链路。

前缀无法确认时登记：

```text
UNKNOWN_PREFIX
```

多网关候选无法消解时登记：

```text
MULTIPLE_GATEWAY_CANDIDATES
```

这两类问题默认阻止提交。

---

## 12. 平台交互流程

M06 提供四类能力：

```text
login()
list_images()
list_flavors()
import_topology()
```

### 12.1 登录

调用：

```text
POST /identity/anonymity/unsafe/login
```

token 从响应 `data` 中提取，并写入：

```text
authorization: <token>
```

### 12.2 镜像查询

调用：

```text
POST /image/list
```

必须分页读取完整目录。

### 12.3 Flavor 查询

调用：

```text
POST /flavor/listBySpace
```

候选必须满足镜像最低资源要求。

### 12.4 拓扑导入

调用：

```text
POST /projects/meshTopo/agent/import
```

只提交通过 M07 验证的完整 Payload。

---

## 13. Payload 编译

M07 通过统一 ID 注册表生成：

| 对象 | 建议前缀 |
|---|---|
| VM | `V` |
| 二层交换机 | `W` |
| 三层交换机 | `T` |
| NIC | `P` |
| Link | `L` |
| Network | `G` |
| Subnet | `S` |

编译顺序：

```text
1. 写入外部 projectId/networkId
2. 创建内部 ID
3. 编译节点
4. 编译 Network
5. 编译 Subnet
6. 编译 NIC
7. 编译 Link
8. 编译区域和布局
9. 补充平台默认字段
10. 执行验证
```

默认：

```text
version = v2
portMappingList = []
```

只有图片或任务输入明确提供端口映射时，才生成端口映射。

---

## 14. 提交前验证

M07 执行五类验证。

### 14.1 字段

- 根字段存在；
- 类型正确；
- 枚举合法；
- `x/y` 为字符串；
- Flavor 字段类型符合平台要求。

### 14.2 引用

- 对象 ID 唯一；
- Link 端点存在；
- Network 成员存在；
- Subnet 引用 Network；
- NIC 引用 Subnet；
- 节点内部 NIC 引用一致。

### 14.3 图结构

- 无完全重复链路；
- 无错误孤立节点；
- 广播域成员与物理连接一致；
- 接口不被异常复用。

### 14.4 IP

- IP 合法；
- CIDR 合法；
- IP 属于子网；
- 网关属于子网；
- 无重复 IP；
- 网关对应三层设备接口。

### 14.5 平台

- `type/devType` 匹配；
- VM 的 `imageId` 来自资源目录；
- Flavor 满足镜像要求；
- `projectId/networkId` 未被改写；
- 端口映射格式合法。

任一 ERROR 存在时，不允许提交。

---

## 15. 自动修复边界

允许的确定性修复：

- 平台字符串字段类型转换；
- 空数组补全；
- 重复内部 ID 重新生成；
- Link 方向统一；
- 完全重复链路删除；
- 根据确定 IP/前缀重新计算 CIDR。

不允许自动修改：

- 节点类型；
- 节点名称；
- IP 数字；
- 链路端点；
- 区域归属；
- 网关候选。

M04 已完成固定的文本融合且不会追加视觉调用。进入 M05 后仍不确定的视觉事实继续保留
为未解析项；阻塞性未解析项必须终止后续编译，不能由 M05 猜测补全。

---

## 16. 运行编排

M08 固定执行：

```text
1. 校验任务输入
2. 加载图片
3. 查询平台资源
4. 调用模型识别
5. 生成 ResolvedTopologyIR
6. 绑定资源
7. 编译 Payload
8. 执行验证
9. 保存 Payload
10. 调用完整导入接口
11. 保存响应和最终状态
```

CLI 形式：

```bash
python -m topology_agent \
  --image ./topology.png \
  --project-id <projectId> \
  --network-id <networkId>
```

---

## 17. 任务状态

只保留六个状态：

| 状态 | 含义 |
|---|---|
| `CREATED` | 任务已创建 |
| `RUNNING` | 正在处理 |
| `VALIDATION_FAILED` | Payload 未通过验证 |
| `SUBMISSION_UNCERTAIN` | 写请求超时，服务端状态未知 |
| `FAILED` | 明确失败 |
| `COMPLETED` | 平台明确导入成功 |

`SUBMISSION_UNCERTAIN` 状态下不得自动重复提交。

---

## 18. 运行产物

每次任务保存：

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

这些是正常业务运行产物，用于：

- 人工验收；
- 错误定位；
- 对比图片与识别结果；
- 检查平台 Payload；
- 查看导入失败原因。

---

## 19. 安全约束

日志不得包含：

- 模型 API Key；
- 平台密码；
- 完整 token。

工程不得包含：

- 样例完整答案；
- 图片哈希到 JSON 的映射；
- 固定工程 ID；
- 固定平台镜像 UUID；
- 根据文件名直接生成拓扑的逻辑。

---

## 20. 样例验收基准

### 20.1 111

期望：

| 对象 | 数量 |
|---|---:|
| 节点 | 9 |
| 链路 | 8 |
| 二层交换机 | 3 |
| 路由器 | 2 |
| 客户端 | 4 |
| Network | 3 |
| Subnet | 3 |

子网：

- `192.168.1.0/24`
- `192.168.2.0/24`
- `192.168.12.0/24`

### 20.2 222

期望：

| 对象 | 数量 |
|---|---:|
| 节点 | 32 |
| 链路 | 31 |
| 二层交换机 | 10 |
| 路由器 | 3 |
| 防火墙 | 1 |
| 客户端 | 12 |
| 服务器类节点 | 6 |
| Network | 10 |
| Subnet | 10 |
| 区域 | 4 |

区域：

- `LAB JARINGAN`
- `RUANG SERVER`
- `MANAJEMENT`
- `CLIENT`

纯图片输入时：

```text
portMappingList = []
```

---

## 21. 架构完成标准

架构实现完成时应满足：

- 一条命令完成图片到平台导入；
- 只有一个逻辑 Agent；
- 只保留三个 Skill；
- Topology IR 与平台 Payload 分离；
- 平台资源由接口实时查询；
- `projectId/networkId` 只来自任务输入；
- Payload 通过五类验证；
- 验证失败时不提交；
- 写请求状态未知时不自动重试；
- 运行产物完整；
- 不包含数据库、队列、后台服务和测试框架。
