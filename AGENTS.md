# AGENTS.md

## 1. 项目目标

本仓库实现单 Agent 的网络拓扑识别链路：

```text
拓扑图片
  → TopologyObservation
  → TopologyIR
  → ResolvedTopologyIR
  → PlatformTopologyPayload
  → 验证
  → 平台完整导入
```

当前按模块顺序开发。除非任务明确要求，否则不得越过当前模块提前实现后续功能。

## 2. 权威来源与优先级

实现前必须阅读以下四份文档，代码和配置应以其为准：

1. `docs/development_plan.md`
2. `docs/architecture.md`
3. `docs/topology_ir.md`
4. `docs/platform_mapping.md`

发生冲突时按以下顺序处理：

1. 当前用户任务中的明确要求；
2. `docs/development_plan.md` 的模块范围和验收标准；
3. `docs/architecture.md` 的职责边界和数据流；
4. `docs/topology_ir.md` 的 IR 字段与语义；
5. `docs/platform_mapping.md` 的平台 Payload 字段与类型。

不得修改四份来源文档来迁就实现。来源未定义的细节采用最小、可替换、不过度抽象的实现，并在最终说明中列出该项假设。

## 3. 固定架构约束

必须遵守：

- 只有一个逻辑 Agent。
- 只保留三个 Skill：`topology_recognition`、`network_reasoning`、`platform_mapping`。
- 大模型只处理视觉事实、候选值和不确定性。
- 程序处理去重、CIDR、网关、资源查询、ID、Payload 编译和验证。
- `projectId`、`networkId` 只来自单次任务输入，不得写入静态配置、生成或改写。
- Topology IR 不包含平台 UUID、镜像、Flavor、`Node.type` 或平台接口对象。
- 平台镜像和 Flavor 必须来自运行时平台查询，禁止固定样例资源 ID。
- 存在阻塞性未解析项时不得编译或提交。
- 验证失败时不得提交。
- 写请求状态不确定时不得自动重试。
- 日志不得包含模型 API Key、平台密码或完整 token。

## 4. 当前开发边界

每次只实现任务点名的模块。未被当前任务要求的模块不得提前编写业务实现。

特别禁止：

- 多 Agent 编排；
- Web 后台或独立 HTTP 服务；
- 数据库、消息队列、任务队列；
- 多模型供应商插件层；
- 完整 trace、metrics、replay 系统；
- 自动创建平台工程；
- 自动生成 `projectId` 或 `networkId`；
- 图片不可见的 OSPF、静态路由、NAT、端口映射、描述或运维属性；
- 无限修复循环；
- 为未来需求预留抽象基类、适配器、注册中心或插件目录。

仅当至少存在两个当前实际实现时，才允许引入抽象基类。

## 5. Python 与 `.venv` 规则

开发和运行必须使用仓库根目录的 `.venv`。

允许的命令形式：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/python -m topology_agent
```

禁止：

- 使用系统 Python 直接运行项目；
- 使用系统 `pip` 或裸 `pip`；
- 使用 Conda 环境替代 `.venv`；
- 把 `.venv` 提交到版本库。

所有验收命令必须显式使用 `.venv/bin/python`。依赖增加后更新 `pyproject.toml`，并将当前环境的精确依赖写入 `requirements.lock`。

## 6. 文件与模块控制

遵循文档中的约 14 个 Python 文件结构，但只在对应模块开始时创建文件。

- 不为简单类单独创建文件。
- 普通 Python 文件目标为 150～450 行。
- `models.py` 可放宽，但应保持聚合清晰。
- M1 的异常类与模型放在现有 M1 文件中，不单独创建 `errors.py`。
- 不创建空的未来模块，不用 `pass`、`NotImplementedError` 或空函数占位。
- 不创建 `.done`、`M1_COMPLETE`、状态戳、阶段锁、生成标记或其他里程碑标记文件。
- 不在源代码中写大段阶段说明、横幅注释、`# region`、自动生成声明或重复文档内容。
- 注释只解释非显然约束；公共模型字段含义优先通过类型和简短描述表达。
- 不保留未使用导入、死代码、兼容别名或未来扩展参数。

## 7. 测试与验收控制

本项目不建立自动化测试框架。

严禁创建：

- `tests/`；
- `manual_tests/`；
- pytest 配置、fixture、plugin；
- `test_*.py`、`*_test.py`；
- 专门的 smoke-test、check、verify、validation helper 脚本；
- 为了覆盖率而增加的分支或假对象。

允许：

- 按 `docs/development_plan.md` 的当前模块验收表执行少量命令式检查；
- 使用一次性的 shell heredoc 或 `python -c` 验证模型、配置和样例；
- 使用 `compileall`、导入检查和真实样例解析；
- 在 `/tmp` 创建临时文件，命令结束后不纳入仓库。

验收代码不得提交到仓库。不要为了证明简单 getter、枚举或字段默认值而增加测试。

## 8. 样例与防硬编码规则

`111.json`、`222.json` 以及对应图片是验收资料，不是运行时答案库。

禁止：

- 根据文件名、图片哈希或 taskId 选择固定结果；
- 把样例完整 JSON 复制到 Prompt、Skill 或 Python 常量；
- 写死样例节点名称、IP、平台 ID、镜像 ID；
- 针对样例数量写特殊分支；
- 通过放宽所有类型为 `Any` 来让样例“通过”。

样例只用于验证通用模型和通用流程。

## 9. 数据模型规则

- 核心模型集中在 `src/topology_agent/models.py`。
- 使用 Pydantic v2 强类型模型。
- JSON 字段使用文档定义的 camelCase；Python 内部可用 snake_case，但必须通过 alias 正确读写 camelCase。
- 默认拒绝未知字段，只有来源明确允许自由对象时才使用受限字典。
- 集合使用空数组，不用 `null` 替代必需集合。
- 置信度限制在 `0.0～1.0`。
- IPv4、前缀和 CIDR 使用可验证类型或等价验证器。
- 平台 `properties.x/y` 必须是字符串，不得自动把数字宽松转换为字符串。
- Flavor 的 `cpu/ram/disk` 在平台 Payload 中必须是字符串。
- `Node.type` 只允许 `VM`、`SW`、`TSW`。
- 不维护独立手写 JSON Schema；从 Pydantic 模型生成。
- M1 只做单对象字段级验证，不提前实现 M7 的完整跨对象引用验证器。

## 10. 配置规则

- 非敏感配置放在 `config/app.yaml` 和 `config/device_mapping.yaml`。
- 模型 API Key、平台用户名和平台密码从环境变量读取。
- `.env.example` 只列变量名和空值，不包含真实密钥。
- `projectId`、`networkId` 不得出现在 `config/app.yaml`。
- 配置缺失或类型错误必须启动即失败，并给出字段路径。
- Secret 值不得出现在 `repr`、日志或异常拼接文本中。

## 11. 修改流程

执行任务时：

1. 阅读 `AGENTS.md` 和四份来源文档中的相关章节。
2. 检查当前树和已有实现，避免重复创建。
3. 明确当前模块允许修改的文件。
4. 先实现最小闭环，再执行模块验收。
5. 只修复与当前模块验收直接相关的问题。
6. 删除临时代码、调试输出和未使用内容。
7. 最终检查没有越界文件、测试目录、TODO 或敏感信息。

不得只给计划而不修改文件；也不得在未读取现有文件时整仓重写。

## 12. 完成报告格式

完成后仅报告：

1. 已修改或新增的文件；
2. 已实现的当前模块能力；
3. 实际执行的 `.venv` 验收命令及结果；
4. 来源文档未明确而采用的最小假设；
5. 明确说明未实现的后续模块。

不要粘贴整份源代码，不要生成冗长测试报告，不要声称未实际执行的命令已通过。