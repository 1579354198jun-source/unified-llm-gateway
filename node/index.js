/**
 * unified-llm —— 多模型统一接入层（Node 18+，原生 fetch，零依赖）
 *
 *   export LLM_BASE_URL="https://你的接口地址/v1"
 *   export LLM_API_KEY="sk-xxxxxx"
 *   node example.js
 */

const BASE_URL = (process.env.LLM_BASE_URL || 'https://api.example.com/v1').replace(/\/+$/, '');
const API_KEY = process.env.LLM_API_KEY || '';

/** 价格表：元 / 百万 token，按你的渠道价覆盖 */
const PRICING = {
  'deepseek-v3': { in: 2.0, out: 8.0 },
  'deepseek-r1': { in: 4.0, out: 16.0 },
  'qwen-max': { in: 20.0, out: 60.0 },
  'qwen-plus': { in: 4.0, out: 12.0 },
  'glm-4-plus': { in: 50.0, out: 50.0 },
  'kimi-k2': { in: 4.0, out: 16.0 },
};
const DEFAULT_PRICE = { in: 10.0, out: 30.0 };

/**
 * 错误分类 —— 切换模型的前提是"判断这个错误值不值得切"。
 * 参数错、内容被拒：换成哪一家结果都一样，直接抛。
 * 超时、限流、5xx、渠道级问题（key 失效、模型不存在）：换一家很可能就成了。
 */
class UnifiedLLMError extends Error {}
class RetryableError extends UnifiedLLMError {}
class FatalError extends UnifiedLLMError {}

/** 可重试 / 可切换的状态码 */
const RETRYABLE_STATUS = new Set([401, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504]);
/** 确定性失败的状态码：请求本身有问题，换谁都一样 */
const FATAL_STATUS = new Set([400, 405, 413, 415, 422]);

function classifyStatus(status, model, body = '') {
  const detail = `[${model}] HTTP ${status}: ${String(body).slice(0, 200)}`;
  return FATAL_STATUS.has(status) ? new FatalError(detail) : new RetryableError(detail);
}

class UnifiedLLM {
  constructor({ apiKey = API_KEY, baseUrl = BASE_URL, models = ['deepseek-v3', 'qwen-max', 'glm-4-plus'], timeout = 60000, maxRetries = 2, retryDelay = 1000 } = {}) {
    if (!apiKey || baseUrl.includes('example.com')) {
      throw new Error('未配置接口地址或密钥，请设置环境变量 LLM_BASE_URL / LLM_API_KEY');
    }
    this.apiKey = apiKey;
    this.baseUrl = baseUrl;
    this.models = models;
    this.timeout = timeout;
    this.maxRetries = maxRetries;
    this.retryDelay = retryDelay;
    /** @type {Array<{model:string,promptTokens:number,completionTokens:number,ts:number}>} */
    this.usage = [];
  }

  _headers() {
    return { Authorization: `Bearer ${this.apiKey}`, 'Content-Type': 'application/json' };
  }

  _buildMessages(prompt, messages) {
    if (messages) return messages;
    if (prompt) return [{ role: 'user', content: prompt }];
    throw new Error('prompt 和 messages 至少要给一个');
  }

  async _request(model, messages, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);
    try {
      let res;
      try {
        res = await fetch(`${this.baseUrl}/chat/completions`, {
          method: 'POST',
          headers: this._headers(),
          body: JSON.stringify({ model, messages, ...options }),
          signal: controller.signal,
        });
      } catch (e) {
        // 连不上、超时中断 —— 都算临时性失败
        throw new RetryableError(`[${model}] 网络异常：${e && e.message}`);
      }
      if (!res.ok) throw classifyStatus(res.status, model, await res.text());
      return res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  /** 一次对话；不指定 model 时按顺序自动切换 */
  async chat(prompt, { messages, model, failover = true, ...options } = {}) {
    const msgs = this._buildMessages(prompt, messages);
    const candidates = model ? [model] : this.models;
    let lastErr;

    for (const m of candidates) {
      for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
        try {
          const data = await this._request(m, msgs, options);
          this._record(m, data.usage || {});
          return data.choices[0].message.content;
        } catch (e) {
          // 请求本身有问题，换一家、再重试都没意义
          if (e instanceof FatalError) throw e;
          lastErr = e;
          if (attempt < this.maxRetries) {
            await new Promise((r) => setTimeout(r, this.retryDelay * (attempt + 1)));
          }
        }
      }
      if (!failover) break;
    }
    throw new Error(`所有候选模型均调用失败，最后一次错误：${lastErr && lastErr.message}`);
  }

  /** 流式输出 */
  async *stream(prompt, { messages, model, ...options } = {}) {
    const msgs = this._buildMessages(prompt, messages);
    const m = model || this.models[0];
    const res = await fetch(`${this.baseUrl}/chat/completions`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify({ model: m, messages: msgs, stream: true, ...options }),
    });
    if (!res.ok) throw classifyStatus(res.status, m, await res.text());

    const decoder = new TextDecoder();
    let buffer = '';
    for await (const chunk of res.body) {
      buffer += decoder.decode(chunk, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        const s = line.trim();
        if (!s.startsWith('data:')) continue;
        const payload = s.slice(5).trim();
        if (payload === '[DONE]') return;
        try {
          const delta = JSON.parse(payload).choices[0].delta;
          if (delta && delta.content) yield delta.content;
        } catch { /* 跳过心跳等非 JSON 行 */ }
      }
    }
  }

  _record(model, usage) {
    this.usage.push({
      model,
      promptTokens: usage.prompt_tokens || 0,
      completionTokens: usage.completion_tokens || 0,
      ts: Date.now(),
    });
  }

  _cost(rec) {
    const p = PRICING[rec.model] || DEFAULT_PRICE;
    return (rec.promptTokens * p.in + rec.completionTokens * p.out) / 1_000_000;
  }

  /** 用量与成本汇总 */
  report() {
    const byModel = {};
    for (const r of this.usage) {
      const b = (byModel[r.model] ||= { 调用次数: 0, 输入: 0, 输出: 0, 成本: 0 });
      b.调用次数 += 1;
      b.输入 += r.promptTokens;
      b.输出 += r.completionTokens;
      b.成本 += this._cost(r);
    }
    for (const v of Object.values(byModel)) v.成本 = Number(v.成本.toFixed(4));
    return {
      '总调用次数': this.usage.length,
      '总 tokens': this.usage.reduce((s, r) => s + r.promptTokens + r.completionTokens, 0),
      '总成本_元': Number(this.usage.reduce((s, r) => s + this._cost(r), 0).toFixed(4)),
      '按模型拆分': byModel,
    };
  }

  resetUsage() {
    this.usage = [];
  }
}

module.exports = {
  UnifiedLLM,
  PRICING,
  UnifiedLLMError,
  RetryableError,
  FatalError,
  classifyStatus,
  RETRYABLE_STATUS,
  FATAL_STATUS,
};
