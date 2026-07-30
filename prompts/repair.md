# 一次有限结构修复

输入包含原始模型输出、解析或校验错误、同一个领域 Skill 和同一个目标 JSON Schema。
本次调用只修复结构，不重新观察图片，也不重新执行拓扑识别。

## 允许修复

- 修复 JSON 括号、引号、逗号和转义，使输出成为合法 JSON 对象。
- 移除 JSON 对象之外的 Markdown 标记或解释文字。
- 将字段名改为 Schema 定义的 camelCase 名称。
- 修复不符合 Schema pattern 的 ID 前缀，并同步修改所有对该 ID 的引用。
- Evidence ID 使用 `evidence_` 前缀；unresolved ID 使用 `unresolved_` 前缀。
- 将值改成 Schema 要求且原响应已有依据的字段类型。
- 恢复原响应已经表达但放错层级的字段。
- 补充 Schema 要求的结构性容器。
- 对原响应明确表示为空的集合使用 `[]`。
- 对 Schema 明确允许且原响应无法确认的标量使用 `null`。
- 对无法可靠确认的内容保留或补入 Schema 允许的 unresolved 结构。

## 禁止修复

- 不重新分析图片；本次请求默认不包含图片。
- 不增加原响应没有依据的新节点、链路、接口、IP、区域或 Evidence。
- 不改变原响应中的视觉文字来使拓扑更合理。
- 不补充图片不可见的接口、IP、前缀、CIDR、网关或网络配置。
- 不删除候选值或不确定性来强行通过校验。
- 不把 `unknown` 随机改成具体设备类型。
- 不创建平台 UUID、`projectId`、`networkId`、`imageId` 或 Flavor。
- 不生成平台 Payload、路由、NAT、策略或端口映射。
- 不使用固定样例答案替换原响应。

## 保留原则

- 保留原始 Evidence 及其可用字段。
- 保留对象已有的 `evidenceIds` 和 `sourceViewIds`。
- 若修复 `evidenceId`，必须对所有 `evidenceIds` 引用执行同一重命名，不能只改定义或只改引用。
- 保留原始候选项、置信度和 unresolved 状态。
- 原响应中无可靠值时，使用 Schema 允许的最小结构。
- required 字段无法在不编造事实的情况下填充时，优先使用允许的空数组、`null` 或 unresolved 表达。
- 如果 Schema 不允许安全的最小值，不编造样例事实；保持最接近原响应的合法表达。

## 输出检查

1. 输出必须是一个合法 JSON 对象。
2. 字段名必须与 Schema 的 camelCase 名称一致。
3. 不得输出 Schema 之外字段。
4. 不得输出 Markdown 代码块。
5. 不得解释修改过程。
6. 不得输出思考过程或 `reasoning_content`。
7. 修复只执行这一次，直接返回最终对象。
