# 平台语义核对 Skill

本 Skill 只用于核对语义设备类型与平台对象类别、`devType` 和资源角色之间的语义候选。
依据当前任务提供的语义对象、平台映射规则和运行时资源候选进行解释，不生成真实平台 ID，不调用平台接口。

## 语义类型与平台对象职责

- `client` 对应 VM 类客户端语义，候选 `Node.type=VM`、`devType=CLIENT`。
- 通用及细分服务器语义对应 VM 类服务器，候选 `Node.type=VM`、`devType=SERVER`。
- `router` 对应 VM 类动态路由设备，候选 `devType=DRT`。
- `public_router` 仅在语义明确时对应公共路由角色，候选 `devType=PRT`。
- `firewall` 对应 VM 类防火墙，候选 `devType=FW`。
- `ids`、`waf`、`des` 只在语义明确时对应各自 VM 设备类型。
- `switch_l2` 对应平台二层交换机 `SW/SW`，不绑定普通 VM 镜像。
- `unknown` 没有可提交的平台类型，必须保持阻塞，不能伪造 VM、SW 或 TSW。

该映射只解释语义候选；最终平台对象由程序依据 `config/device_mapping.yaml` 编译。

## devType 与 resourceRole

- `devType` 表达平台节点的设备类别，不是视觉证据。
- `resourceRole` 表达运行时镜像和 Flavor 匹配角色，不是镜像 ID。
- 相同 `Node.type=VM` 的设备可拥有不同 `devType` 和资源角色。
- 服务器细分类别可共享 `SERVER`，但保留各自资源角色和匹配关键词。
- 二层交换机不应因为缺少镜像而转换成普通 VM。
- 语义类型与配置映射冲突时报告冲突，不自行改写输入语义类型。
- `unknown` 或缺失映射必须阻止编译，而不是使用通用服务器兜底。

## 运行时资源候选

- 镜像和 Flavor 只能从调用方提供的本次运行时平台查询结果中选择。
- 模型可解释候选镜像名称、`nodeType`、系统类型、架构和关键词为何匹配。
- 模型可解释候选 Flavor 是否满足调用方提供的最低 CPU、RAM、Disk 条件。
- 模型不能发明未在候选目录中的镜像或 Flavor。
- 模型不能生成、补齐、变形或猜测 `imageId`。
- 模型不能把固定样例资源 ID 当作候选。
- 无合法候选时必须报告阻塞，不允许空 `imageId` 继续提交。
- 最终候选排序、资源存在性和最小浪费选择由程序执行。

## 外部与内部 ID 边界

- `projectId` 和 `networkId` 只来自单次任务输入。
- 模型不得生成、改写、规范化或替换这两个外部 ID。
- 平台节点、NIC、Link、Network 和 Subnet ID 由程序统一生成。
- 平台 UUID、镜像 UUID 和 Flavor 标识不得来自模型。
- 模型不得从名称、文件名、taskId 或哈希推导任何 ID。

## 平台字段类型

- `Node.type` 只允许 `VM`、`SW`、`TSW`。
- `properties.devType` 必须符合目标平台枚举。
- 平台 `properties.x` 和 `properties.y` 必须为字符串。
- 平台 Flavor 的 `cpu`、`ram`、`disk` 必须为字符串。
- 必需集合使用数组，不使用 `null`。
- VM 的镜像和 Flavor 字段来自程序确认的 ResourceBinding。
- 二层交换机的业务 `nicList` 按平台规则为空，不通过模型虚构 NIC。
- 平台字段的最终强类型校验由程序完成。

## Network、Subnet、NIC 与 Link 语义

- 广播域可编译为 Network 和 Subnet，但最终关系来自已解析 IR。
- Network 的锚点和成员由程序计算，不由模型猜测。
- NIC 的 `subnetId` 必须引用程序生成的实际 Subnet。
- Link 端点和接口引用必须来自已验证 IR。
- 区域映射到节点属性，不生成区域节点或区域链路。
- 模型不能直接构造引用 ID 或完整平台对象。

## 提交边界

- 模型不登录平台、不查询 token、不发写请求。
- 模型不直接编译或提交 Payload。
- 模型不绕过字段、引用、图、IP 和平台资源验证。
- 存在 `unknown`、资源缺失或阻塞性未解析项时不能建议继续提交。
- 验证失败时不能建议忽略错误。
- 写请求状态不确定时不能建议自动重试。

## 禁止事项

- 不生成固定 `projectId`、`networkId`、镜像 ID、Flavor ID 或平台 UUID。
- 不复制完整平台样例 JSON。
- 不根据样例文件名、图片哈希或预期节点数量选择平台对象。
- 不把视觉模型推断当作平台实时资源事实。
- 不把图片中不可见的 OSPF、静态路由、NAT、端口映射、描述或策略加入平台字段。
- 不返回可直接提交的完整 Payload。

输出只解释语义候选、冲突、资源匹配理由和阻塞原因，并严格服从当前 Schema。
