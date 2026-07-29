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
  → 结构化视觉识别
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

- 查看全图和必要局部裁剪；
- 识别节点、文字、接口、IP、链路和区域；
- 输出符合数据模型约束的结构化结果；
- 对少量低置信度区域进行一次局部复核。

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
视觉事实识别       → 多模态模型
候选值与不确定性   → 多模态模型
节点和链路去重     → 程序
CIDR 和网关计算    → 程序
平台资源查询       → 程序
内部对象 ID        → 程序
平台 JSON 编译     → 程序
提交前验证         → 程序
完整拓扑导入       → 程序
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
| M02 | 图片预处理 | `image.py` | 图片加载、缩放、切片、裁剪、坐标转换 |
| M03 | Prompt、Skill 与模型调用 | `llm.py`、`prompts/`、`skills/` | 多模态调用、结构化输出、有限重试 |
| M04 | 拓扑视觉识别 | `recognition.py` | 全图和局部识别，生成 TopologyObservation |
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

- 原图信息；
- 全局缩放图；
- 重叠切片；
- 局部裁剪；
- 原图和切片坐标映射；
- 图片哈希。

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
  → 保真副本
  → 全局缩放
  → 必要切片
  → 坐标映射
```

要求：

- 保持宽高比；
- 不删除细线；
- 不把区域边框处理成拓扑链路；
- 任何切片坐标都能映射回原图；
- 只进行一次保守增强。

不依赖复杂的传统视觉算法来决定拓扑结构。

---

## 9. 模型调用流程

M03 提供统一函数，例如：

```text
call_structured(prompt, images, response_model)
```

调用规则：

1. 系统提示词加载通用约束；
2. 识别请求加载 `topology_recognition`；
3. 需要网络歧义辅助时加载 `network_reasoning`；
4. 平台语义核对时加载 `platform_mapping`；
5. 响应必须通过数据模型解析；
6. 429、502、503、504 可有限重试；
7. 参数错误、认证错误和非法响应不进行无意义重试。

调用预算：

```text
全图识别：1 次
局部补充：最多 2 批
局部修复：最多 1 次
```

超过预算直接失败，不进入无限循环。

---

## 10. 视觉识别流程

M04 先查看全图，提取：

- 区域；
- 节点；
- 节点名称；
- 设备类型；
- 主要链路；
- 接口文字；
- IP 文字。

随后对低置信度区域进行局部补充。

合并时保留：

- 原始文字；
- 候选值；
- 置信度；
- 边界框；
- 来源视图；
- 证据。

模型不得自动生成：

- OSPF；
- 静态路由；
- NAT；
- 端口映射；
- 运维描述；
- 图片中不可见的系统配置。

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

这些视觉或语义事实只能通过一次局部复核解决；仍不确定则终止。

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
