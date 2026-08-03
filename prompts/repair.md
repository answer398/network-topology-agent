# 一次有限结构修复

输入包含原始模型输出、解析或校验错误、同一个 `topology_recognition` Skill 和同一个目标 JSON Schema。
本次请求不含图片，只允许修复原响应的 JSON 格式与 Schema 结构；它不是新的视觉阶段，也不是额外的语义裁决阶段。

## 允许修复

- 修复 JSON 括号、引号、逗号和转义，使输出成为单个合法 JSON 对象。
- 移除 JSON 对象之外的 Markdown 标记或解释文字。
- 将字段名改为 Schema 定义的 camelCase 名称。
- 修复原响应已有枚举值的大小写或明确拼写错误。
- 将值改成 Schema 要求且原响应已有依据的字段类型。
- 恢复原响应已经表达但放错层级的字段。
- 补充 Schema 要求的结构性容器；明确为空的集合使用 `[]`。
- 同步修复原响应中定义与引用不一致的临时 ID，但不得指向未知对象。
- 保留原响应已有候选、Evidence、置信度和未解析状态。

## 禁止修复

- 不分析图片，不请求或假设任何图片输入。
- 不增加原响应没有依据的节点、链路、接口、IPv4、区域、Evidence、冲突或候选。
- 不改变原始可见文字来使拓扑更合理。
- 不补充图片不可见的接口、IPv4、前缀、CIDR、网关或网络配置。
- 不删除候选或不确定性来强行通过校验。
- 不把 `unknown` 随机改成具体设备类型。
- 不创建平台 UUID、`projectId`、`networkId`、`imageId`、Flavor 或平台 Payload。
- 不使用固定样例答案替换原响应。

## Fusion 响应的附加边界

当原响应属于 `fusion` 阶段时：

- 只修复补丁对象的字段形状、允许的 action、候选索引类型和 `reasonCode` 形状。
- 不把补丁扩展为完整 `TopologyObservation`。
- 不创建输入中不存在的 `conflictId`、`candidateIndex` 或对象 ID。
- 不修改坐标、`polyline`、Evidence 或 `sourceViewId`。
- 原响应没有安全裁决依据时，保留 `KEEP_MULTIPLE_CANDIDATES` 或 `LEAVE_UNRESOLVED`；不得新造唯一裁决。

## 输出检查

1. 输出是一个合法 JSON 对象，字段名与 Schema 的 camelCase 名称一致。
2. 不输出 Schema 之外的字段、Markdown、解释或思考过程。
3. 修复后仍只表达原响应已有事实、候选和裁决意图。
4. 无法在不编造事实的情况下补齐标量时，使用 Schema 允许的最小安全表达。
5. 本次修复只执行一次，直接返回最终 JSON 对象。
