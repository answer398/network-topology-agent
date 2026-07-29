# 平台拓扑映射与接口适配规范

## 1. 文档目的

本文定义 ResolvedTopologyIR 到平台标准拓扑 JSON 的转换规则，包括：

- 平台登录；
- 镜像查询；
- Flavor 查询；
- 节点类型映射；
- Network、Subnet、Node、NIC 和 Link 的引用关系；
- 区域和坐标；
- 内部对象 ID；
- 提交前验证；
- 标准拓扑导入。

接口适配实现集中在：

```text
src/topology_agent/platform.py
src/topology_agent/compiler.py
src/topology_agent/validator.py
```

---

## 2. 平台交互边界

平台接口包括：

| 接口 | 用途 |
|---|---|
| `/identity/anonymity/unsafe/login` | 登录并获取 token |
| `/image/list` | 查询镜像 |
| `/flavor/listBySpace` | 查询 Flavor |
| `/projects/meshTopo/agent/import` | 完整拓扑导入 |

工程不负责：

- 创建平台工程；
- 创建根网络；
- 生成 `projectId`；
- 生成 `networkId`；
- 逐节点创建；
- 逐链路创建。

运行流程：

```text
操作人员在平台界面创建工程
  → 取得 projectId 和 networkId
  → 将两者作为任务输入
  → 生成完整 Payload
  → 调用标准拓扑导入接口
```

---

## 3. 登录接口

请求：

```text
POST /identity/anonymity/unsafe/login
```

请求体示例：

```json
{
  "username": "test",
  "password": "your_password",
  "rememberMe": true
}
```

响应示例：

```json
{
  "code": 1,
  "data": "token-value",
  "success": true,
  "failed": false
}
```

token 读取位置：

```text
data
```

后续请求头：

```text
authorization: <token>
```

成功判断不能只检查单个字段，应综合：

- HTTP 状态；
- `code`；
- `success`；
- `failed`；
- `data`；
- `message`。

允许的成功码放入配置文件。

---

## 4. 镜像查询

请求：

```text
POST /image/list
```

请求头：

```text
authorization: <token>
```

分页参数：

| 参数 | 类型 | 默认值 |
|---|---|---|
| `pageIndex` | int | 1 |
| `pageSize` | int | 10 |

可使用字段筛选：

- `imageName`
- `status`
- `virtualization`
- `visibility`
- `osType`
- `osVersion`
- `platformId`
- `platformType`
- `hardwareArchitecture`
- `nodeType`
- `id`

响应：

```json
{
  "code": 1,
  "message": "success",
  "data": {
    "total": 100,
    "items": []
  },
  "success": true,
  "failed": false
}
```

主要镜像字段：

| 字段 | 说明 |
|---|---|
| `id` | 镜像 ID |
| `imageName` | 镜像名称 |
| `status` | 镜像状态 |
| `osType` | 操作系统类型 |
| `osVersion` | 操作系统版本 |
| `virtualization` | 虚拟化类型 |
| `minDisk` | 最小磁盘 |
| `minRam` | 最小内存 |
| `platformType` | 平台类型 |
| `platformId` | 平台标识 |
| `hardwareArchitecture` | 硬件架构 |
| `nodeType` | 设备类型 |
| `tag` | 标签 |
| `description` | 描述 |

规则：

- 分页读取到 `data.total`；
- 不能只读取第一页；
- 只使用当前平台可用镜像；
- `imageId` 必须来自本次查询结果；
- 未找到合法镜像时停止任务。

---

## 5. Flavor 查询

请求：

```text
POST /flavor/listBySpace
```

Body：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | Flavor ID |
| `flavorName` | string | 名称 |
| `cpu` | int | CPU |
| `disk` | int | 磁盘 GB |
| `ram` | int | 内存 MB |

响应：

```json
{
  "code": 1,
  "message": "success",
  "data": {
    "total": 10,
    "items": [
      {
        "id": "RS00-001-000128-00010",
        "flavorName": "通用型 4C 8G",
        "cpu": 4,
        "disk": 80,
        "ram": 8192
      }
    ]
  },
  "success": true,
  "failed": false
}
```

绑定规则：

- `ram >= image.minRam`；
- `disk >= image.minDisk`；
- CPU、RAM、Disk 满足设备策略；
- 从满足条件的候选中选择资源浪费较小者。

平台 Payload 中 Flavor 字段使用字符串：

```json
{
  "cpu": "1",
  "ram": "1024",
  "disk": "40"
}
```

编译时必须显式转换。

---

## 6. 标准拓扑导入

请求：

```text
POST /projects/meshTopo/agent/import
```

请求头：

```text
authorization: <token>
```

根 Payload：

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

其中：

- `projectId` 来自任务输入；
- `networkId` 来自任务输入；
- 编译器不得生成或修改这两个 ID；
- `version` 默认 `v2`；
- 无明确端口映射时 `portMappingList=[]`。

---

## 7. 根对象

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `projectId` | String | 是 | 目标工程 ID |
| `networkId` | String | 是 | 目标根网络 ID |
| `version` | String | 否 | 模板版本 |
| `networkList` | List | 是 | Network 配置 |
| `subnetList` | List | 是 | Subnet 配置 |
| `nodeList` | List | 是 | 节点配置 |
| `linkList` | List | 是 | 链路配置 |
| `portMappingList` | List<String> | 否 | 端口映射 |

空集合使用：

```json
[]
```

不使用 `null` 代替必需集合。

---

## 8. Node.type

| `type` | 含义 | 对应 devType |
|---|---|---|
| `VM` | 虚拟机类节点 | VM 设备类型 |
| `SW` | 二层交换机 | `SW` |
| `TSW` | 三层交换机 | `TSW` |

VM 的 `devType`：

| devType | 含义 |
|---|---|
| `SERVER` | 服务器 |
| `CLIENT` | 客户端 |
| `DRT` | 动态路由 |
| `FW` | 防火墙 |
| `IDS` | 入侵检测 |
| `WAF` | Web 应用防火墙 |
| `PRT` | 公共动态路由 |
| `DES` | DES 设备 |

---

## 9. 设备语义映射

| IR semanticType | Node.type | properties.devType | 资源角色 |
|---|---|---|---|
| `client` | `VM` | `CLIENT` | PC/客户端 |
| `server` | `VM` | `SERVER` | 通用服务器 |
| `web_server` | `VM` | `SERVER` | Web 服务器 |
| `database_server` | `VM` | `SERVER` | 数据库服务器 |
| `mail_server` | `VM` | `SERVER` | 邮件服务器 |
| `monitor_server` | `VM` | `SERVER` | 监控服务器 |
| `vpc_server` | `VM` | `SERVER` | VPC 服务器 |
| `router` | `VM` | `DRT` | 路由器 |
| `public_router` | `VM` | `PRT` | 公共路由 |
| `firewall` | `VM` | `FW` | 防火墙 |
| `ids` | `VM` | `IDS` | IDS |
| `waf` | `VM` | `WAF` | WAF |
| `des` | `VM` | `DES` | DES |
| `switch_l2` | `SW` | `SW` | 不绑定普通 VM 镜像 |
| `switch_l3` | `TSW` | `TSW` | 按三层交换机规则 |

---

## 10. Node 对象

主要字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | String | `VM/SW/TSW` |
| `properties` | Object | 节点属性 |
| `nicList` | List | 网卡 |
| `secPolicyCmd` | List<String> | 安全策略 |
| `routeTable` | List | 静态路由 |

默认不生成图片不可见的安全策略和静态路由。

### 10.1 二层交换机

推荐：

```json
{
  "type": "SW",
  "nicList": [],
  "properties": {
    "id": "W...",
    "devType": "SW",
    "nodeName": "Switch1",
    "x": "0",
    "y": "0"
  }
}
```

### 10.2 VM

VM 需要：

- `imageId`；
- `imageName`；
- `sysType`；
- `flavor`；
- `nicList`。

资源字段必须来自 ResourceBinding。

---

## 11. PropertiesConfig

主要字段：

| 字段 | 说明 |
|---|---|
| `id` | 节点 ID |
| `devType` | 设备类型 |
| `nodeName` | 节点名称 |
| `description` | 描述 |
| `x/y` | 平台坐标字符串 |
| `district` | 区域名称 |
| `fillColor` | 区域颜色 |
| `imageId/imageName` | VM 镜像 |
| `sysType` | 系统类型 |
| `flavor` | VM 配额 |
| `userData` | 用户数据 |
| `metadata` | 元数据 |
| `otherAttributeList` | 自定义属性 |

使用已验证字段组合：

```json
{
  "singleNetwork": false,
  "transparent": 0,
  "otherAttributeList": []
}
```

VM 默认：

```json
{
  "metadata": [],
  "userData": ""
}
```

不得把图片中不存在的 OSPF、描述和运维属性填入这些字段。

---

## 12. NicConfig

主要字段：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `id` | 是 | NIC ID |
| `name` | 是 | 网卡名称 |
| `subnetId` | 是 | Subnet ID |
| `ip` | 是 | IP 地址 |
| `macAddress` | 否 | MAC |
| `bandwidth` | 否 | 带宽 |
| `packetLossRate` | 否 | 丢包率 |
| `delay` | 否 | 延迟 |

约束：

- `ip` 只写地址，不写前缀；
- 前缀写入 `SubnetConfig.cidr`；
- 同一节点内 NIC 名称唯一；
- `subnetId` 指向存在的 Subnet；
- NIC ID 在整个请求中唯一。

---

## 13. NetworkConfig

主要字段：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `id` | 是 | Network ID |
| `name` | 是 | Network 名称 |
| `mtu` | 否 | MTU |
| `nodeId` | 是 | 绑定交换机节点 ID |
| `vlan` | 否 | VLAN |
| `transmitNodeIdList` | 否 | 广播域其他成员节点 |

默认关系：

> 一个二层交换机对应一个 NetworkConfig。

编译规则：

```text
NetworkConfig.nodeId
  = 二层交换机节点 ID

NetworkConfig.transmitNodeIdList
  = 广播域中除交换机外的节点 ID
```

名称建议：

```text
sg-<交换机名称>
```

默认：

```text
mtu = 1350
```

---

## 14. SubnetConfig

主要字段：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `id` | 是 | Subnet ID |
| `name` | 是 | 名称 |
| `cidr` | 是 | CIDR |
| `gatewayIp` | 否 | 网关 |
| `dns` | 否 | DNS |
| `networkId` | 是 | Network ID |
| `dhcpPool` | 否 | DHCP 地址池 |
| `enableDhcp` | 否 | DHCP 开关 |

推荐默认：

```json
{
  "dns": "",
  "enableDhcp": true
}
```

名称建议：

```text
sn-<交换机名称>
```

引用：

```text
SubnetConfig.networkId
  → NetworkConfig.id

NicConfig.subnetId
  → SubnetConfig.id
```

---

## 15. LinkConfig

主要字段：

| 字段 | 说明 |
|---|---|
| `id` | Link ID |
| `sDevId` | 起点设备 |
| `sNicName` | 起点接口 |
| `dDevId` | 目标设备 |
| `dNicName` | 目标接口 |

交换机连接 VM、路由器或防火墙时：

```text
sDevId = 交换机 ID
dDevId = 对端节点 ID
dNicName = 对端接口名称
```

交换机侧通常不填写 `sNicName`。

要求：

- Link ID 唯一；
- 节点引用存在；
- 接口名称存在；
- 无完全重复链路；
- Link 方向统一。

---

## 16. 区域映射

RegionIR 不生成平台节点。

映射：

```text
RegionIR.normalizedName
  → properties.district

RegionIR.fillColor
  → properties.fillColor
```

规则：

- 区域成员节点使用相同 `district`；
- 颜色格式为 `R,G,B`；
- 区域框不加入 `nodeList`；
- 区域边框不加入 `linkList`。

已知区域示例：

- `LAB JARINGAN`
- `RUANG SERVER`
- `MANAJEMENT`
- `CLIENT`

---

## 17. 坐标映射

平台 `properties.x/y` 使用字符串。

流程：

```text
原图中心坐标
  → 平移到画布中心
  → 等比例缩放
  → 适配平台坐标方向
  → 最小间距处理
  → 转为字符串
```

要求：

- 保持相对左右关系；
- 保持相对上下关系；
- 节点不严重重叠；
- 不要求与参考 JSON 坐标逐值相同。

---

## 18. 资源绑定

模型只提供：

- `semanticType`；
- `vendorModel`；
- `resourceRole`；
- `imageKeywords`。

程序从镜像目录选择真实资源。

匹配优先级：

1. 设备语义类别；
2. `nodeType`；
3. 镜像名称和型号关键词；
4. 系统类型；
5. 架构；
6. 镜像状态；
7. 最低资源要求。

禁止：

- 使用固定样例 `imageId`；
- 模型生成 UUID；
- 使用空 `imageId` 提交；
- 找不到资源时继续编译。

---

## 19. 内部对象 ID

外部输入：

```text
projectId
networkId
```

内部生成：

| 对象 | 前缀 |
|---|---|
| VM 节点 | `V` |
| 二层交换机 | `W` |
| 三层交换机 | `T` |
| NIC | `P` |
| Link | `L` |
| Network | `G` |
| Subnet | `S` |

要求：

- 请求内唯一；
- 所有编译逻辑共用一个 ID 注册表；
- 子模块不得独立生成引用 ID；
- 外部 ID 不进入内部 ID 生成器。

---

## 20. 端口映射

根字段：

```text
portMappingList
```

只在以下情况生成：

- 图片明确提供；
- 或任务输入明确提供；
- 设备、IP 和端口合法。

默认：

```json
[]
```

参考样例中存在但图片不可直接支持的映射，不应自动复制。

---

## 21. 图片不可见配置

默认不生成：

- OSPF；
- 静态路由；
- NAT；
- 防火墙策略；
- 端口映射；
- 长设备描述；
- 运维自定义属性；
- 管理账号；
- 图片之外的系统配置。

允许生成：

- 图片直接可见字段；
- 根据 IP 和前缀确定性计算的 CIDR；
- 根据三层接口确定的网关；
- 平台要求的固定默认字段；
- 实时查询得到的镜像和 Flavor。

---

## 22. 编译顺序

```text
1. 校验外部 projectId/networkId
2. 建立内部 ID 注册表
3. 编译节点 ID
4. 编译 Network ID
5. 编译 Subnet ID
6. 编译 NIC
7. 编译 Node
8. 编译 Link
9. 编译 Network
10. 编译 Subnet
11. 编译区域和坐标
12. 生成 portMappingList
13. 补充默认字段
14. 执行验证
```

---

## 23. 提交前验证

### 23.1 字段

- 根字段存在；
- 集合字段为数组；
- `type/devType` 枚举合法；
- `x/y` 为字符串；
- Flavor 字段为字符串。

### 23.2 引用

- Node ID 唯一；
- NIC ID 唯一；
- Link ID 唯一；
- Network ID 唯一；
- Subnet ID 唯一；
- Link 端点存在；
- Network 成员存在；
- Subnet 引用 Network；
- NIC 引用 Subnet。

### 23.3 图结构

- 无完全重复链路；
- 广播域成员与物理连接一致；
- 图片中的非孤立节点不被编译为孤立节点；
- 接口没有异常复用。

### 23.4 IP

- IP 合法；
- CIDR 合法；
- IP 属于 CIDR；
- 网关属于 CIDR；
- 无重复 IP；
- 网关对应三层接口。

### 23.5 平台

- `type/devType` 匹配；
- VM `imageId` 来自镜像目录；
- Flavor 合法；
- 外部 ID 未被改写；
- 端口映射格式合法。

任何 ERROR 存在时不调用导入接口。

---

## 24. 样例映射基准

### 24.1 111

| 对象 | 数量 |
|---|---:|
| Node | 9 |
| Link | 8 |
| Network | 3 |
| Subnet | 3 |
| Port Mapping | 0 |

类型：

| 类型 | 数量 |
|---|---:|
| `SW/SW` | 3 |
| `VM/DRT` | 2 |
| `VM/CLIENT` | 4 |

子网：

- `192.168.1.0/24`
- `192.168.2.0/24`
- `192.168.12.0/24`

### 24.2 222

| 对象 | 数量 |
|---|---:|
| Node | 32 |
| Link | 31 |
| Network | 10 |
| Subnet | 10 |

类型：

| 类型 | 数量 |
|---|---:|
| `SW/SW` | 10 |
| `VM/DRT` | 3 |
| `VM/FW` | 1 |
| `VM/CLIENT` | 12 |
| `VM/SERVER` | 6 |

纯图片输入：

```text
portMappingList = []
```

---

## 25. 样例差异处理

参考 JSON 只用于确认：

- 平台对象结构；
- 字段类型；
- 设备类型映射；
- Network、Subnet、NIC 和 Link 的引用关系。

不要求保持一致：

- 随机 ID；
- 数组顺序；
- 精确平台坐标；
- 图片不可见配置；
- 样例中的明显命名错误。

编译时以：

```text
图片事实
+ Topology IR
+ 平台接口文档
+ 平台实时资源
```

作为依据。

---

## 26. 导入响应状态

| 状态 | 含义 |
|---|---|
| `SUCCESS` | 平台明确成功 |
| `FAILED` | 平台明确业务失败 |
| `AUTH_FAILED` | token 无效 |
| `PLATFORM_ERROR` | 平台异常 |
| `SUBMISSION_UNCERTAIN` | 写请求超时且无法判断是否已写入 |

`SUBMISSION_UNCERTAIN` 时不得自动重复提交。

---

## 27. 最终原则

平台适配必须保证：

- 外部工程 ID 不被生成或修改；
- 平台资源来自实时查询；
- 平台对象由程序确定性生成；
- 所有跨对象引用一致；
- 图片不可见配置不被虚构；
- 验证失败时不提交；
- 提交使用一次完整拓扑导入。
