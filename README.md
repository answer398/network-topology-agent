# network-topology-agent

一个面向网络拓扑图片的单 Agent 识别与平台导入工具。当前代码已经把识别层和平台适配层合并，并提供统一 CLI：输入 `runtime/topo/` 下的图片，生成平台格式的 `result.json`，再由操作人员通过命令行绑定 `projectId`、`networkId` 并提交。

## 当前状态

当前可运行链路是：

```text
拓扑图片
  -> ImageBundle 图片预处理
  -> structure 全图结构识别
  -> links 全图链路识别
  -> text 全图文字识别
  -> 程序预融合
  -> fusion 纯文本语义融合
  -> TopologyObservation 强校验
  -> 从 data/ 离线快照读取镜像和 Flavor
  -> 平台 Payload 转换与校验
  -> clearTopo
  -> topology import
```

长期设计文档定义的目标数据流仍然是：

```text
TopologyObservation
  -> TopologyIR
  -> ResolvedTopologyIR
  -> ResourceBinding
  -> PlatformTopologyPayload
```

其中 `models.py` 已定义了这些核心契约，但当前 CLI 仍由已合并的 `api/platform.py` 从 `TopologyObservation` 直接完成平台转换。规划中的独立 `topology.py`、`platform.py`、`compiler.py`、`validator.py`、`orchestrator.py` 和 `artifacts.py` 尚未作为独立模块实现，README 不将它们描述为当前已完成能力。

## 核心原则

- 只有一个逻辑 Agent，不做多 Agent 编排。
- 只保留三个 Skill：`topology_recognition`、`network_reasoning`、`platform_mapping`。
- 模型只负责图片中的视觉事实、候选值、置信度和不确定性。
- 程序负责坐标转换、去重、接口绑定、CIDR、广播域、网关、资源查询、平台 ID、Payload 编译和校验。
- `projectId`、`networkId` 只接受单次 `--submit` 命令的输入，不写入静态配置，也不自动生成。
- 镜像和 Flavor 由独立刷新命令获取，识别阶段只从 `data/` 下的资源快照离线映射；不把资源 ID 写死在代码中。
- 存在阻塞性未解析项或 Payload 校验失败时，不发送平台导入请求。
- 平台写请求的传输状态不确定时，不自动重试。
- 日志和提交响应会脱敏，不写入模型 API Key、平台密码或完整 token。

## 环境要求

- Python 3.11 或更高版本。
- 可访问模型兼容接口和平台 API。
- 仓库根目录的 `.venv`。项目开发和运行都不使用系统 Python、裸 `pip` 或 Conda。

## 安装

在仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

依赖由 `pyproject.toml` 管理，当前包括 OpenAI 兼容客户端、HTTP、Pillow、Pydantic、PyYAML、dotenv 和 requests。固定依赖记录在 `requirements.lock`。

## 配置

复制环境变量模板：

```bash
cp .env.example .env
```

在 `.env` 中填写：

```dotenv
TOPOLOGY_MODEL_API_KEY=
TOPOLOGY_PLATFORM_USERNAME=
TOPOLOGY_PLATFORM_PASSWORD=
```

也可以直接设置 shell 环境变量。shell 中已有的值优先于 `.env`。模型 Key 只在识别和样例 harness 中必需；平台用户名和密码只在刷新资源快照或提交时必需：

- `TOPOLOGY_MODEL_API_KEY`：多模态模型 API Key。
- `TOPOLOGY_PLATFORM_USERNAME`：平台登录用户名。
- `TOPOLOGY_PLATFORM_PASSWORD`：平台登录密码。

识别阶段固定读取 `config/app.yaml` 指定的离线资源快照，默认只能使用
`data/list_images.json` 和 `data/list_flavors.json`，不会登录平台或在线查询资源。
这两个路径必须位于仓库的 `data/` 目录内。需要更新资源时，使用 CLI 的
`--refresh-resources` 参数；提交阶段仍然必须在线登录平台。

`config/app.yaml` 保存非敏感配置，包括：

- 模型 Base URL、模型名、Token 上限、thinking 开关与预算、超时和调用预算；
- 平台 Base URL、登录、资源查询、清理拓扑和导入接口路径；
- 图片最长边限制；
- 默认版本、MTU、DHCP 和 DNS；
- 运行相关配置。

`config/device_mapping.yaml` 保存语义设备类型到平台 `Node.type`、`devType` 和镜像关键词的映射。不要把真实密钥、平台密码、`projectId` 或 `networkId` 写入 YAML。

如果平台地址或接口路径与当前 `config/app.yaml` 不同，应先修改对应的非敏感字段。配置加载会校验字段类型和路径，缺少密钥或配置错误会在启动阶段失败。

## 统一 CLI

### 1. 更新资源快照（按需执行）

```bash
.venv/bin/python topology_cli.py --refresh-resources
```

该命令登录平台，分页获取完整的镜像和 Flavor 列表，并覆盖写入：

```text
data/list_images.json
data/list_flavors.json
```

它不会识别图片，也不会清理或导入平台拓扑。刷新日志写入
`runtime/resource_refresh/log/run.log`。如果平台当前不可达，原有快照保持不变。

### 2. 放入图片

把待识别图片放在 `runtime/topo/` 下。支持 `.png`、`.jpg` 和 `.jpeg`，推荐使用文件名作为任务名：

```text
runtime/topo/challenge1.png
```

CLI 接受简单文件名或文件名 stem。任务名只能包含字母、数字、`.`、`_`、`-`，同一 stem 如果匹配到多张图片会直接失败。

### 3. 识别并生成 result.json

```bash
.venv/bin/python topology_cli.py -recognize challenge1
```

也支持长选项和模块入口：

```bash
.venv/bin/python topology_cli.py --recognize challenge1
.venv/bin/python -m topology_agent --recognize challenge1
```

识别命令会依次执行：

1. 读取并预处理图片。
2. 只从 `data/list_images.json` 和 `data/list_flavors.json` 读取可用镜像和 Flavor。
3. 执行四次逻辑模型调用：`structure`、`links`、`text` 和纯文本 `fusion`。
4. 将 `TopologyObservation` 交给平台适配层转换为 Payload。
5. 校验节点、网卡、网络、子网、链路、CIDR、IP 和资源绑定。
6. 写入不含外部工程 ID 的 `result.json`。

识别阶段不会写入 `projectId` 和 `networkId`。内部生成的节点、网卡、网络、子网和链路 ID 只是本次 Payload 的内部对象 ID，不等同于平台工程 ID，也不保证不同运行之间保持不变。

### 4. 提交到平台

先在平台界面创建或确认工程，取得对应的 `projectId` 和 `networkId`，再执行：

```bash
.venv/bin/python topology_cli.py --submit challenge1 projectId=xxxx networkId=xxx
```

`-submit` 也可用：

```bash
.venv/bin/python topology_cli.py -submit challenge1 projectId=xxxx networkId=xxx
```

提交命令会：

1. 读取 `runtime/challenge1/result.json`。
2. 检查两个 ID 是否各提供且非空，并拒绝与文件中已有值冲突的输入。
3. 将本次命令的两个 ID 绑定到 Payload。
4. 使用 Pydantic 模型和平台适配层再次校验完整 Payload。
5. 持久化绑定后的 `result.json`。
6. 登录平台，先清理目标拓扑，再调用导入接口。
7. 保存提交状态和脱敏后的平台响应。

平台导入接口当前是“先清理、后导入”。因此必须确认 `projectId` 和 `networkId` 正确。清理或导入发生网络超时、连接中断等情况时，命令会记录 `SUBMISSION_UNCERTAIN`，不会自动重试；应先到平台确认实际状态，再决定后续操作。

## 运行产物

对 `challenge1.png` 执行识别后，目录大致如下：

```text
runtime/
├── topo/
│   └── challenge1.png
└── challenge1/
    ├── result.json
    └── log/
        ├── run.log
        └── attempt_001/
            ├── recognition.jsonl
            ├── global_structure.png
            ├── global_links.png
            ├── global_text.png
            ├── structure_context.json
            ├── links_context.json
            ├── text_context.json
            ├── fusion_context.json
            ├── structure_model_responses.json
            ├── links_model_responses.json
            ├── text_model_responses.json
            ├── fusion_model_responses.json
            ├── structure_evidence.json
            ├── links_evidence.json
            ├── text_evidence.json
            ├── fusion_patch.json
            └── topology_observation.json
```

不同失败点可能只产生部分文件。再次识别不会覆盖旧的 `attempt_###`，而是创建下一个编号的 attempt。

文件用途：

- `result.json`：平台适配层生成的最终 Payload。识别后不含 `projectId/networkId`，提交后会包含本次绑定的值。
- `run.log`：CLI 层的 JSONL 日志，记录识别、资源查询、提交准备、失败和成功状态。
- `recognition.jsonl`：识别层按阶段记录的 JSONL 日志，包含调用次数、HTTP 请求数、Token 统计、阶段状态和产物事件。
- `global_*.png`：实际发送给视觉阶段的完整图片视图，用于复核图片预处理和坐标。
- `*_context.json`：发送前的任务和结构化上下文，不保存图片 data URL、密钥或完整 Prompt。
- `*_model_responses.json`：各阶段的模型响应记录。
- `*_evidence.json`：视觉阶段产出的 Evidence 快照。
- `fusion_patch.json`：纯文本 Fusion 阶段的结构化补丁。
- `topology_observation.json`：通过识别层强校验后的平台无关观察结果，不是平台 Payload。

提交后还会在任务目录生成：

```text
runtime/challenge1/submission_result.json
runtime/challenge1/submission_response.json
```

其中 `submission_result.json` 包含 `COMPLETED`、`FAILED`、`VALIDATION_FAILED` 或 `SUBMISSION_UNCERTAIN` 等状态；平台返回后才生成 `submission_response.json`，内容会进行截断和敏感字段脱敏。

## Agent 设计

### 四阶段识别

识别层固定使用三次完整全图视觉调用和一次纯文本融合调用：

| 阶段 | 输入 | 负责内容 | 不负责内容 |
| --- | --- | --- | --- |
| `structure` | 完整结构视图 | 节点主体、粗粒度设备类型、区域、节点位置和名称候选 | 链路推理、CIDR、平台字段 |
| `links` | 带稳定节点编号的完整视图 | 可见连线、折线路径、端点候选、交叉状态、遗漏节点 | 网络语义、资源匹配 |
| `text` | 保守增强后的完整视图 | 可见节点名、接口名、IPv4、前缀、区域文字及空间归属候选 | 猜测不可见配置 |
| `fusion` | 三个阶段的结构化文本摘要 | 对已有冲突进行有限裁决，或保留多候选和未解析项 | 新建对象、修改几何、补造 Evidence、读取图片 |

三个视觉阶段都覆盖整张拓扑图，不切片。Fusion 不发送第四张图片。坐标以当前视图中心为原点，程序在生成 Observation 前负责坐标映射、归一化、去重和引用检查。

### 三个 Skill

- `topology_recognition`：视觉识别和当前 Fusion 约束。当前 `recognize_topology` 的四次调用实际使用这个 Skill。
- `network_reasoning`：广播域、CIDR、网关和网络一致性规则。Skill 文件和调用契约已经保留，但当前 CLI 的网络推导由 Python 平台适配层执行。
- `platform_mapping`：平台节点、网络、子网、链路和资源映射规则。Skill 文件已经保留，但当前 CLI 不让模型生成平台 Payload，映射由 `api/platform.py` 执行。

`prompts/system.md`、`prompts/extraction.md` 和 `prompts/repair.md` 共同约束模型输出。模型返回的字段会通过 Pydantic Schema 校验，低置信度候选和 `unresolvedItems` 不会为了生成完整 Payload 而被静默删除。

### 程序侧职责

程序在模型调用之外完成：

- EXIF 纠正、RGB 转换、图片增强和全图视图生成；
- 临时 ID、Evidence、坐标和跨对象引用校验；
- 节点、接口和链路去重及绑定；
- 从可见接口地址推导 CIDR、广播域、网关和必要的网络配置；
- 根据设备语义查询并选择当前平台的镜像和 Flavor；
- 生成平台 Payload、验证字段类型和引用关系；
- 控制提交顺序、写请求状态和日志脱敏。

不会从图片之外生成 OSPF、静态路由、NAT、端口映射、安全策略或运维描述。

## 平台适配层

`api/platform.py` 提供 `TopologyPlatformClient` 以及对应的函数式封装，当前使用 `config/app.yaml` 中的接口路径：

| 能力 | 作用 |
| --- | --- |
| 登录 | 获取平台 token，并在授权失败时重新登录 |
| 镜像查询 | 分页读取可用镜像目录 |
| Flavor 查询 | 分页读取 Flavor 目录 |
| `formatData` | 将 Observation 转为平台 Payload |
| `validate_payload` | 校验 Payload 的字段、引用、IP、CIDR 和拓扑关系 |
| `import_topology` | 清理目标拓扑并提交完整导入请求 |

设备映射的典型结果如下：

| 语义类型 | 平台 `Node.type` | 平台 `devType` |
| --- | --- | --- |
| `switch_l2` | `SW` | `SW` |
| `switch_l3` | `TSW` | `TSW` |
| `router` | `VM` | `DRT` |
| `firewall` | `VM` | `FW` |
| `client` | `VM` | `CLIENT` |
| `server` 及服务器变体 | `VM` | `SERVER` |

VM 节点的镜像先按资源目录中的兼容类型和名称、厂商型号、设备关键词进行匹配，再从满足镜像最小 RAM/Disk 要求的 Flavor 中选择资源。没有可匹配镜像或 Flavor 时，识别命令失败，不生成可提交结果。

平台 Payload 使用 camelCase 字段，主要集合包括 `networkList`、`subnetList`、`nodeList`、`linkList` 和 `portMappingList`。`Node.type` 只接受 `VM`、`SW`、`TSW`；平台坐标和 Flavor 的 `cpu/ram/disk` 使用字符串格式；IPv4、CIDR、网卡和引用关系均进行强校验。

## Harness 与样例验收

仓库当前没有独立的 Web harness、测试框架或数据库任务系统。样例 harness 是根目录的 `run_sample_recognition.py`，用于直接验证识别层，不经过平台登录、资源查询和提交。

样例图片位于：

```text
runtime/runs/111.png
runtime/runs/222_v1.png
```

运行两个样例：

```bash
.venv/bin/python run_sample_recognition.py
```

只运行一个样例：

```bash
.venv/bin/python run_sample_recognition.py 111
.venv/bin/python run_sample_recognition.py 222
```

这个 harness：

- 从 `config/app.yaml` 读取模型和图片配置；
- 为每个样例创建一个最多四次逻辑调用的 `OpenAICompatibleModelClient`；
- 调用 `load_image_bundle` 和 `recognize_topology`；
- 在标准输出打印每个样例的状态、调用统计和 Observation 摘要；
- 在 `runtime/runs/` 写入带 UTC 时间戳的 `sample_recognition_summary_*.json`；
- 识别层默认在 `runtime/runs/<taskId>/attempt_###/` 保存阶段产物。

它只需要模型 Key；由于通过包导入加载 dotenv，仓库 `.env` 也可以提供该变量。真实模型调用是否成功取决于当前模型地址、Key、网络和图片内容。

`data/list_images.json` 和 `data/list_flavors.json` 是识别阶段唯一使用的 JSON 快照，使用
`--refresh-resources` 更新，并保持平台接口返回的对象字段。`data/111.json`、`data/222.json` 是平台 Payload 样例，`data/topology_observation_1.json`、`data/topology_observation_2.json` 是 Observation 样例。它们只用于人工验收模型和适配规则，运行时不会根据文件名、哈希或 taskId 选择固定答案，也不会作为模型 Prompt 的答案库。

基础检查可以使用一次性命令完成，不需要创建测试目录：

```bash
.venv/bin/python -m compileall -q src api topology_cli.py
.venv/bin/python -m topology_agent --help
```

需要验证平台适配层时，应使用真实或受控的 HTTP 响应检查登录、分页资源查询、Payload 校验和导入状态；当前仓库没有自动化 pytest 测试套件。

## 常见失败状态

| 现象 | 常见原因 | 处理方式 |
| --- | --- | --- |
| 配置加载失败 | 模型 Key 为空、YAML 字段缺失或类型错误 | 检查 `.env`、`config/app.yaml` 和错误中的字段路径 |
| 找不到图片 | stem 不匹配、扩展名不支持或同名图片多于一张 | 检查 `runtime/topo/`，每个任务名只保留一张图片 |
| 识别阶段失败 | 模型调用超时、响应截断、结构化输出不合法或调用预算不足 | 查看 `log/attempt_###/recognition.jsonl` 和阶段上下文 |
| 无法生成 `result.json` | 存在阻塞性未解析项、接口没有有效 IP/前缀、拓扑引用不完整 | 先处理识别歧义或图片信息，再重新识别 |
| 资源绑定失败 | 离线快照没有兼容镜像、满足要求的 Flavor | 先执行 `--refresh-resources`，再检查 `device_mapping.yaml` |
| 资源快照刷新失败 | 平台登录失败、平台不可达或接口响应格式错误 | 检查平台凭据、网络和 `runtime/resource_refresh/log/run.log`；识别仍可使用旧快照 |
| `VALIDATION_FAILED` | `result.json` 字段、IP、CIDR、ID 或引用不合法 | 查看 `submission_result.json`，不要直接提交未经修复的 JSON |
| `platform login failed; topology write was not attempted` | 当前机器无法访问平台登录地址或平台连接失败 | 检查 `platform.baseUrl`、网络路由和平台服务状态；登录失败不会发起拓扑写入 |
| `SUBMISSION_UNCERTAIN` | 清理或导入请求的网络状态未知 | 先确认平台实际拓扑状态，不要盲目重复提交 |

## 直接调用 Python API

CLI 是推荐入口。需要在代码中调用平台适配层时，可以使用：

```python
from api import TopologyPlatformClient, validate_payload

client = TopologyPlatformClient()
client.login(platform_username, platform_password)
try:
    images = client.list_images()
    flavors = client.list_flavors()
    payload = client.formatData(
        observation,
        None,
        None,
        image_items=images,
        flavor_items=flavors,
    )
    validate_payload({**payload, "projectId": project_id, "networkId": network_id})
finally:
    client.close()
```

生产代码仍应使用 `--submit` 的绑定和提交流程，避免把工程 ID 写进识别阶段或静态配置。更完整的适配层说明见 [`docs/api.md`](docs/api.md)。

## 目录结构

```text
network-topology-agent/
├── topology_cli.py                 # 根目录 CLI 包装入口
├── run_sample_recognition.py       # 111/222 样例识别 harness
├── pyproject.toml                  # 工程和依赖
├── requirements.lock               # 当前环境依赖锁定记录
├── config/
│   ├── app.yaml                    # 非敏感运行配置
│   └── device_mapping.yaml         # 设备语义到平台类型的映射
├── prompts/
│   ├── system.md
│   ├── extraction.md
│   └── repair.md
├── skills/
│   ├── topology_recognition.md
│   ├── network_reasoning.md
│   └── platform_mapping.md
├── src/topology_agent/
│   ├── __main__.py                 # python -m topology_agent
│   ├── cli.py                      # 识别和提交编排
│   ├── config.py                   # YAML、dotenv 和强配置模型
│   ├── image.py                    # 图片预处理和坐标转换
│   ├── llm.py                      # OpenAI 兼容模型客户端
│   ├── models.py                   # Observation、IR、Payload 和状态模型
│   └── recognition.py              # 四阶段识别和 Observation 强校验
├── api/
│   ├── platform.py                 # 平台登录、资源查询、转换、校验和导入
│   └── example.py                  # 适配层示例
├── data/                           # 人工验收样例，不是运行时答案库
├── docs/                           # 架构、IR、平台映射和开发计划
└── runtime/                        # 被 gitignore 的图片、日志和运行产物
```

## 明确未实现

以下能力仍属于开发计划或后续模块，不应从当前 README 推断为已完成：

- 独立的 Topology IR 规范化、网络推理和跨对象验证编排；
- 独立的资源绑定、Payload 编译器、验证器、任务编排器和统一 artifacts 模块；
- 自动创建平台工程，或自动生成 `projectId`、`networkId`；
- 多 Agent、多模型供应商插件、Web 后台、独立 HTTP 服务、数据库、消息队列和任务队列；
- OSPF、静态路由、NAT、端口映射以及图片不可见的运维属性；
- 自动化 pytest 测试框架、完整 trace/metrics/replay 系统和无限修复循环。

四份设计来源文档仍是实现边界和字段语义的权威来源：

- [`docs/development_plan.md`](docs/development_plan.md)
- [`docs/architecture.md`](docs/architecture.md)
- [`docs/topology_ir.md`](docs/topology_ir.md)
- [`docs/platform_mapping.md`](docs/platform_mapping.md)
