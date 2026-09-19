# -*- coding: utf-8 -*-
"""使用示例：把下面四个场景跑一遍，你就知道这套封装值不值。"""
from unified_llm import UnifiedLLM

# 候选模型按顺序排列，前一个不通就自动切下一个
llm = UnifiedLLM(models=["deepseek-v3", "qwen-max", "glm-4-plus"])

# 1) 最简调用
print("— 1. 基础对话 —")
print(llm.chat("用一句话解释什么是向量数据库"))

# 2) 指定模型 + 系统提示词
print("\n— 2. 带系统提示词 —")
print(llm.chat(
    prompt="帮我写一封催款邮件，语气客气但不软弱",
    model="qwen-max",
    extra={"messages": [
        {"role": "system", "content": "你是一名商务沟通顾问，中文输出，不超过 200 字。"},
        {"role": "user", "content": "帮我写一封催款邮件，语气客气但不软弱"},
    ]},
))

# 3) 流式输出（适合接入前端打字机效果）
print("\n— 3. 流式输出 —")
for chunk in llm.stream("讲个关于程序员和需求的冷笑话"):
    print(chunk, end="", flush=True)
print()

# 4) 用量与成本
print("\n— 4. 用量与成本 —")
import json
print(json.dumps(llm.report(), ensure_ascii=False, indent=2))

# 5) 导出明细，落到你自己的计费系统
llm.export_usage("usage.jsonl")
print("\n明细已导出到 usage.jsonl")
