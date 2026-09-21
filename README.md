# unified-llm-gateway

一套代码管住所有大模型：统一调用、自动切换、用量可算、成本可查。

Python 版 + Node 版，协议层 OpenAI 兼容，接哪家都能跑。

---

## 为什么会有这个东西

接大模型这件事，**接第一个很开心，从第二个开始就烦了**。

| 你接了 N 个模型之后 | 实际发生的事 |
|---|---|
| 每家一套 SDK | 业务代码里塞满 if-else |
| 每家一份账单 | 月底对账对到半夜 |
| 每家一种错误码 | 重试逻辑写了三遍 |
| 其中一家挂了 | 你的服务跟着一起挂 |
| 老板问这个月 AI 花了多少钱 | 你答不上来 |

这个仓库干的事，就是把上面那张表压成一个 client。

## 特性

- **统一入口** —— 一个 `chat()` 打天下，不用记每家的参数差异
- **自动切换** —— 候选模型按顺序排，前一个不可用自动切下一个，业务代码零改动
- **错误分类** —— 确定性失败（参数错、内容被拒）直接抛出，不浪费切换次数；临时性失败（超时、限流、5xx）自动重试并切换下一家
- **用量统计** —— 调用次数、输入输出 token、成本，按模型拆开，一键导出 JSONL
- **流式输出** —— 原生支持打字机效果
- **成本可算** —— 内置价格表，按你自己的渠道价覆盖即可
- **零依赖** —— Node 版用原生 fetch，Node 18+ 直接跑，不用装任何包

## 快速开始

### Python

```bash
pip install -r python/requirements.txt

export LLM_BASE_URL="https://你的接口地址/v1"
export LLM_API_KEY="sk-xxxxxx"

python python/example.py
```

```python
from unified_llm import UnifiedLLM

llm = UnifiedLLM(models=["deepseek-v3", "qwen-max", "glm-4-plus"])

# 最简调用，自动按顺序选可用模型
print(llm.chat("用一句话解释什么是向量数据库"))

# 流式
for chunk in llm.stream("讲个冷笑话"):
    print(chunk, end="", flush=True)

# 用量与成本
print(llm.report())
```

### Node

```bash
export LLM_BASE_URL="https://你的接口地址/v1"
export LLM_API_KEY="sk-xxxxxx"

node node/example.js
```

```js
const { UnifiedLLM } = require('./node/index.js');

const llm = new UnifiedLLM({ models: ['deepseek-v3', 'qwen-max', 'glm-4-plus'] });
console.log(await llm.chat('用一句话解释什么是向量数据库'));

for await (const chunk of llm.stream('讲个冷笑话')) process.stdout.write(chunk);

console.log(llm.report());
```

## API

| 方法 | 说明 |
|---|---|
| `chat(prompt, {messages, model, failover, ...})` | 同步对话。不指定 `model` 时按候选顺序自动切换；`failover=False` 则只试第一个 |
| `stream(prompt, {messages, model, ...})` | 流式输出，逐段产出文本 |
| `report()` | 返回调用次数、总 tokens、总成本、按模型拆分 |
| `export_usage(path)` | 用量明细导出为 JSONL，方便落库到自有计费系统 |
| `reset_usage()` | 清空累计用量（例如按天归档后重置） |

### 覆盖价格表

内置价格只是量级参考，按你的实际渠道价覆盖：

```python
from unified_llm import PRICING
PRICING["deepseek-v3"] = {"in": 1.8, "out": 7.2}
```

```js
const { PRICING } = require('./node/index.js');
PRICING['deepseek-v3'] = { in: 1.8, out: 7.2 };
```

### 异常处理

切换模型的前提是判断"这个错误值不值得切"。本库把错误分成两类：

| 异常 | 含义 | 处理方式 |
|---|---|---|
| `FatalError` | 请求本身有问题（400 / 405 / 413 / 415 / 422），换谁都一样 | 直接抛出，不重试、不切换 |
| `RetryableError` | 临时性或渠道级问题（401 / 403 / 404 / 408 / 409 / 425 / 429 / 5xx / 网络异常） | 按 `max_retries` 重试，仍失败则切换下一个候选模型 |

```python
from unified_llm import UnifiedLLM, FatalError, RetryableError

try:
    llm.chat("你好")
except FatalError as e:
    ...       # 参数或内容问题，改请求本身
except RetryableError as e:
    ...       # 所有候选都挂了，走降级逻辑
```

```js
const { UnifiedLLM, FatalError, RetryableError } = require('./node/index.js');
```

`max_retries` 与 `retry_delay` 可调；`chat(..., failover=False)` 可关闭自动切换，只试第一个候选。

## 目录结构

```
unified-llm-gateway/
├── README.md
├── python/
│   ├── unified_llm.py     # 核心实现
│   ├── example.py         # 四个可跑的场景
│   └── requirements.txt
└── node/
    ├── index.js           # 核心实现（零依赖）
    └── example.js
```

## 接入之后：让模型真正懂你的业务

统一接入解决的是「连得上、算得清」。但通用模型 ≠ 能上岗的员工——要让它真的替企业干活，还得做三件事：

- **梳理业务规则** —— 把这家公司的答复口径、流程边界写成模型能执行的规则
- **构建专属知识库** —— 产品资料、历史工单、常见问答喂进去，回答才是这家公司的答案
- **配置工作流** —— 什么消息自动回、什么情况转人工、哪些任务定时跑

已经跑通的场景：

| 场景 | 做法 | 人工参与 |
|---|---|---|
| 客服 7×24 值守 | 常见问题自动应答，复杂问题转人工 | 只做兜底 |
| 社群 / 私域消息回复 | 进消息自动识别意图并回复 | 只做把关 |
| 定时自动化任务 | 日报、台账、会议纪要按时生成 | 只做审核 |

一句话：**接入层负责让 AI 用得上，落地层负责让 AI 用得上手。**

## 企业接入

开源版是给你自己维护的接入层。如果不想自己维护，或者有下面这些需求：

- 多模型统一开通与管理，一个后台、一份账单
- 人民币结算，可对公开票
- 渠道热备与故障切换，避免单点故障导致全线停摆
- 用量明细可导出，按部门 / 项目拆分成本
- 接入阶段有专人对接，跑通再谈长期

这些属于技术服务范畴，可以在 Issues 留言，或直接联系维护者。

**联系方式**：邮箱 `1579354198@qq.com`，或在本仓库 [Issues](../../issues) 留言，看到就回。

## License

MIT
