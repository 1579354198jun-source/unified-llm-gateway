const { UnifiedLLM } = require('./index.js');

(async () => {
  const llm = new UnifiedLLM({ models: ['deepseek-v3', 'qwen-max', 'glm-4-plus'] });

  console.log('— 1. 基础对话 —');
  console.log(await llm.chat('用一句话解释什么是向量数据库'));

  console.log('\n— 2. 带系统提示词 —');
  console.log(await llm.chat(null, {
    model: 'qwen-max',
    messages: [
      { role: 'system', content: '你是一名商务沟通顾问，中文输出，不超过 200 字。' },
      { role: 'user', content: '帮我写一封催款邮件，语气客气但不软弱' },
    ],
  }));

  console.log('\n— 3. 流式输出 —');
  for await (const chunk of llm.stream('讲个关于程序员和需求的冷笑话')) {
    process.stdout.write(chunk);
  }
  console.log('\n');

  console.log('— 4. 用量与成本 —');
  console.log(JSON.stringify(llm.report(), null, 2));
})();
