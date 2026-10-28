"""发言调度策略：决定下一个谁说话。

新增策略：继承 Scheduler，实现 choose()，再在 SCHEDULERS 里注册。
返回 None 表示本轮讨论结束。@点名 的优先逻辑在基类里统一处理，所有策略都生效。
"""
from __future__ import annotations

import json
import random
import re
from typing import Dict, List, Optional

from .config import AgentConfig, AppConfig
from .models import USER, Message
from .providers import Provider, ProviderError


def find_mentions(text: str, agents: List[AgentConfig]) -> List[str]:
    """按出现顺序返回文本里被 @ 的成员名。"""
    hits = []
    for a in agents:
        pos = text.find("@" + a.name)
        if pos >= 0:
            hits.append((pos, a.name))
    return [name for _, name in sorted(hits)]


def last_agent_speaker(history: List[Message]) -> Optional[str]:
    # 包含发言失败的成员，这样轮流策略会跳过出错的人，而不是反复选他
    for m in reversed(history):
        if m.role == "agent" and m.done:
            return m.speaker
    return None


def brief(a: AgentConfig) -> str:
    lines = a.persona.strip().splitlines()
    return a.bio or (lines[0] if lines else "")


def turns_since_user(history: List[Message]) -> int:
    n = 0
    for m in reversed(history):
        if m.role == "user":
            break
        if m.in_context and m.role == "agent":
            n += 1
    return n


class Scheduler:
    name = "base"

    def __init__(self, cfg: AppConfig, providers: Dict[str, Provider], agents: List[AgentConfig], allow_end: bool = True):
        self.cfg = cfg
        self.providers = providers
        self.agents = agents          # 可参与本次选择的成员
        self.allow_end = allow_end    # 是否允许主持人宣布结束

    def agent(self, name: str) -> Optional[AgentConfig]:
        return next((a for a in self.agents if a.name == name), None)

    async def pick(self, history: List[Message]) -> Optional[AgentConfig]:
        if not self.agents:
            return None
        return self.mention_pick(history) or await self.choose(history)

    def mention_pick(self, history: List[Message]) -> Optional[AgentConfig]:
        ctx = [m for m in history if m.in_context]
        if not ctx:
            return None
        def tried_after(msg: Message) -> set:
            # 这条消息之后已经轮到过的成员（含发言失败的），避免反复点名同一个人
            idx = next(i for i, x in enumerate(history) if x is msg)
            return {m.speaker for m in history[idx + 1:] if m.role == "agent" and m.done}

        # 1) 用户最近一条消息 @ 到、且之后还没轮到的成员
        for m in reversed(ctx):
            if m.role == "user":
                tried = tried_after(m)
                for name in find_mentions(m.content, self.agents):
                    if name not in tried:
                        return self.agent(name)
                break
        # 2) 刚发言的 AI 点名了别人（被点名的人如果刚说过话就不强制，避免两人互相 @ 霸占讨论）
        last = ctx[-1]
        if last.role == "agent":
            tried = tried_after(last)
            prev = ctx[-2].speaker if len(ctx) > 1 else None
            for name in find_mentions(last.content, self.agents):
                if name not in (last.speaker, prev) and name not in tried:
                    return self.agent(name)
        return None

    async def choose(self, history: List[Message]) -> Optional[AgentConfig]:
        raise NotImplementedError

    def candidates(self, history: List[Message]) -> List[AgentConfig]:
        """除上一位发言者外的所有成员（只有一个成员时就是他自己）。"""
        ctx = [m for m in history if m.in_context]
        last = ctx[-1].speaker if ctx and ctx[-1].role == "agent" else None
        return [a for a in self.agents if a.name != last] or list(self.agents)


class RoundRobinScheduler(Scheduler):
    name = "round_robin"

    async def choose(self, history):
        last = last_agent_speaker(history)
        names = [a.name for a in self.agents]
        idx = (names.index(last) + 1) % len(names) if last in names else 0
        return self.agents[idx]


class RandomScheduler(Scheduler):
    name = "random"

    async def choose(self, history):
        return random.choice(self.candidates(history))


class ModeratorScheduler(Scheduler):
    """让一个 LLM 读聊天记录，决定下一位发言者或结束讨论。失败时退化为轮流。"""

    name = "moderator"

    async def choose(self, history):
        mod = self.cfg.chat.moderator
        fallback = RoundRobinScheduler(self.cfg, self.providers, self.agents)
        if not mod:
            return await fallback.choose(history)

        cands = self.candidates(history)
        # 最近 4 次发言只在两个人之间来回时，强制换人
        recent = [m.speaker for m in history if m.in_context and m.role == "agent"][-4:]
        if len(recent) == 4 and len(set(recent)) == 2:
            cands = [a for a in cands if a.name not in recent] or cands
        min_turns = mod.min_turns if mod.min_turns is not None else len(self.agents)
        since = turns_since_user(history)
        can_end = self.allow_end and since >= min_turns

        round_msgs = [m for m in history if m.in_context and m.role == "agent"][-since:] if since else []
        counts = "、".join(f"{a.name} {sum(m.speaker == a.name for m in round_msgs)} 次" for a in self.agents)
        members = "\n".join(f"- {a.name}：{brief(a)}" for a in self.agents)
        end_rule = (
            "如果问题已经讨论充分、观点开始重复，或者用户的问题已经得到回答，输出 END 结束讨论。"
            if can_end else "现在讨论还不充分，不能结束，必须选一位成员。"
        )
        system = (
            "你是一个群聊的主持人，负责决定下一位发言者，让讨论有价值、不重复。\n"
            f"成员：\n{members}\n"
            "选人原则：\n"
            "- 本轮还没发言或发言少的成员优先，让每个人的视角都有机会出现。\n"
            "- 不要让两个人连续来回对话超过两个回合，及时引入第三个人的视角。\n"
            "- 被用户点名或质疑的人应该回应；选最能带来新信息的人。\n"
            f"本轮发言次数：{counts}\n"
            f"{end_rule}\n"
            f"候选：{'、'.join(a.name for a in cands)}\n"
            '只输出 JSON，例如 {"next": "名字"}' + (' 或 {"next": "END"}' if can_end else "") + "，不要输出其他内容。"
        )
        transcript = "\n".join(
            f"[{m.speaker}]: {m.content}" for m in [m for m in history if m.in_context][-self.cfg.chat.context_messages:]
        )
        try:
            out = await self.providers[mod.provider].complete(
                mod.model,
                [{"role": "system", "content": system}, {"role": "user", "content": f"聊天记录：\n{transcript}\n\n下一位是谁？"}],
                max_tokens=mod.max_tokens, extra_body=mod.extra_body,
            )
        except ProviderError:
            return await fallback.choose(history)

        choice = self._parse(out, cands)
        if choice == "END":
            return None if can_end else random.choice(cands)
        return choice or await fallback.choose(history)

    @staticmethod
    def _parse(out: str, cands: List[AgentConfig]):
        m = re.search(r"\{.*?\}", out, re.S)
        value = out
        if m:
            try:
                value = str(json.loads(m.group(0)).get("next", ""))
            except (ValueError, AttributeError):
                pass
        if value.strip().upper() == "END":
            return "END"
        for a in cands:
            if a.name in value:
                return a
        return None


SCHEDULERS = {s.name: s for s in (RoundRobinScheduler, RandomScheduler, ModeratorScheduler)}
SCHEDULER_LABELS = {"round_robin": "轮流发言", "random": "随机发言", "moderator": "AI 主持人"}


def build_scheduler(name: str, cfg: AppConfig, providers: Dict[str, Provider], agents: List[AgentConfig], allow_end: bool = True) -> Scheduler:
    return SCHEDULERS.get(name, RoundRobinScheduler)(cfg, providers, agents, allow_end)
