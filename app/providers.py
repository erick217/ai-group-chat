"""模型服务的统一接口。新增服务类型：写一个类实现 stream / complete，再在 PROVIDER_TYPES 里注册。"""
from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any, AsyncIterator, Dict, List

from openai import AsyncOpenAI

Messages = List[Dict[str, str]]


class ProviderError(Exception):
    pass


class Provider:
    async def stream(self, model: str, messages: Messages, temperature: float, max_tokens: int, extra_body: Dict[str, Any] = None) -> AsyncIterator[str]:
        raise NotImplementedError
        yield ""  # pragma: no cover

    async def complete(self, model: str, messages: Messages, temperature: float = 0.2, max_tokens: int = 500, extra_body: Dict[str, Any] = None) -> str:
        parts = []
        async for delta in self.stream(model, messages, temperature, max_tokens, extra_body):
            parts.append(delta)
        return "".join(parts)


class OpenAICompatProvider(Provider):
    """兼容 OpenAI Chat Completions 接口的服务。"""

    def __init__(self, name: str, base_url: str, api_key: str = "", api_key_env: str = "", headers: Dict[str, str] = None, **_: Any):
        self.name = name
        self.base_url = base_url
        self.api_key = api_key or (os.environ.get(api_key_env, "") if api_key_env else "")
        self.api_key_env = api_key_env
        self.headers = headers or {}
        self._client = None

    @property
    def client(self) -> AsyncOpenAI:
        if not self.api_key:
            raise ProviderError(f"provider「{self.name}」缺少 API key：请在 .env 里设置 {self.api_key_env or 'api_key'}")
        if self._client is None:
            self._client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key, default_headers=self.headers)
        return self._client

    async def stream(self, model, messages, temperature, max_tokens, extra_body=None):
        try:
            resp = await self.client.chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_tokens=max_tokens, stream=True,
                extra_body=extra_body or None,
            )
            got_content, finish = False, None
            async for chunk in resp:
                if not chunk.choices:
                    continue
                finish = chunk.choices[0].finish_reason or finish
                if chunk.choices[0].delta.content:
                    got_content = True
                    yield chunk.choices[0].delta.content
            if not got_content and finish == "length":
                raise ProviderError(f"{self.name}/{model} 的思考过程用完了 max_tokens（{max_tokens}），还没开始回答：请调大 max_tokens 或关闭思考")
        except ProviderError:
            raise
        except Exception as e:  # 网络错误、模型不存在、余额不足等
            raise ProviderError(f"{self.name}/{model} 调用失败：{e}") from e

    async def complete(self, model, messages, temperature=0.2, max_tokens=500, extra_body=None):
        try:
            resp = await self.client.chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
                extra_body=extra_body or None,
            )
            return resp.choices[0].message.content or ""
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"{self.name}/{model} 调用失败：{e}") from e


class MockProvider(Provider):
    """假模型：不联网，用来调试界面和调度逻辑。"""

    LINES = [
        "我觉得这个问题可以从两个角度看。第一，**需求是否真实存在**；第二，现有方案为什么没解决它。",
        "补充一点：前面说的有道理，但忽略了成本。如果每个环节都要人工介入，规模化会很难。",
        "我不太同意。历史上很多类似的尝试失败，不是因为想法不好，而是时机不对。",
        "具体怎么做？我建议分三步：\n1. 先做最小原型验证\n2. 找 10 个真实用户试用\n3. 根据反馈决定是否继续",
        "换个角度想，这有点像生态系统里的共生关系，单个物种很难独自存活。",
        "这点我同意，但需要数据支撑，不然只是直觉。",
    ]

    def __init__(self, name: str = "mock", **_: Any):
        self.name = name

    async def stream(self, model, messages, temperature, max_tokens, extra_body=None):
        text = random.choice(self.LINES)
        await asyncio.sleep(0.4)
        for ch in text:
            await asyncio.sleep(0.02)
            yield ch

    async def complete(self, model, messages, temperature=0.2, max_tokens=500, extra_body=None):
        await asyncio.sleep(0.3)
        # 主持人请求：从提示词里拿到候选名单，随机选一个，偶尔结束
        prompt = messages[0]["content"]
        names = []
        if "候选：" in prompt:
            names = prompt.split("候选：", 1)[1].split("\n", 1)[0].split("、")
        if not names or random.random() < 0.15:
            return json.dumps({"next": "END"}, ensure_ascii=False)
        return json.dumps({"next": random.choice(names)}, ensure_ascii=False)


PROVIDER_TYPES = {
    "openai": OpenAICompatProvider,
    "mock": MockProvider,
}


def build_providers(cfg: Dict[str, Dict[str, Any]]) -> Dict[str, Provider]:
    out = {}
    for name, spec in cfg.items():
        spec = dict(spec)
        typ = spec.pop("type", "openai")
        if typ not in PROVIDER_TYPES:
            raise ValueError(f"未知的 provider 类型: {typ}")
        out[name] = PROVIDER_TYPES[typ](name=name, **spec)
    return out
