"""群聊房间：保存聊天记录，按模板推进讨论阶段，驱动 AI 发言，把事件广播给所有打开的网页。"""
from __future__ import annotations

import asyncio
import datetime
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import WebSocket

from .config import FREE_TEMPLATE_KEY, AgentConfig, AppConfig, Phase, Template, load_config
from .models import USER, Message
from .providers import Provider, ProviderError, build_providers
from .schedulers import SCHEDULER_LABELS, brief, build_scheduler, find_mentions
from .search import SearchError, Searcher, build_searcher, format_results, plan_queries

SYSTEM = "系统"
SUMMARY_TASK = "小结本阶段的讨论：用 3–5 条要点列出主要观点、共识、分歧和尚未解决的问题。只做整理，不要加入你自己的新观点。"


def today() -> str:
    return datetime.date.today().strftime("%Y年%m月%d日")


class Room:
    def __init__(self) -> None:
        self.clients: Set[WebSocket] = set()
        self.history: List[Message] = []
        self.task: Optional[asyncio.Task] = None
        self.active = False      # 讨论循环是否在跑（同步维护，避免收尾时漏掉新消息）
        self.kicked = False      # 讨论进行中又收到了新的触发
        self.turns_left = 0      # 自由讨论剩余发言次数
        self.speaking: Optional[str] = None
        self.activity = ""       # searching | typing
        self.template_key = FREE_TEMPLATE_KEY
        self._reset_flow()
        self.cfg: AppConfig
        self.providers: Dict[str, Provider]
        self.searcher: Optional[Searcher] = None
        self.settings: Dict[str, Any] = {}
        self._apply_config(load_config())

    def _apply_config(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.providers = build_providers(cfg.providers)
        self.searcher = build_searcher(cfg.search)
        self.settings = {
            "scheduler": cfg.chat.scheduler,
            "max_turns": cfg.chat.max_turns,
            "search": bool(self.searcher) and cfg.search.enabled,
        }
        if self.template_key not in cfg.templates:
            self.template_key = FREE_TEMPLATE_KEY
            self._reset_flow()

    def _reset_flow(self) -> None:
        self.phase_idx = -1                 # -1：流程未开始；== 阶段数：流程已结束
        self.phase_queue: List[str] = []    # 本阶段还没发言的人（按顺序）
        self.phase_turns_left = 0           # 本阶段由调度器选人的剩余次数
        self.phase_tail: List[str] = []     # 本阶段最后的小结人

    # ---------- 当前模板 / 阶段 ----------

    @property
    def template(self) -> Template:
        return self.cfg.templates[self.template_key]

    @property
    def agents(self) -> List[AgentConfig]:
        return self.template.agents or self.cfg.agents

    def agent(self, name: str) -> Optional[AgentConfig]:
        return next((a for a in self.agents if a.name == name), None)

    @property
    def phase(self) -> Optional[Phase]:
        phases = self.template.phases
        return phases[self.phase_idx] if 0 <= self.phase_idx < len(phases) else None

    def phase_info(self) -> Optional[Dict[str, Any]]:
        phases = self.template.phases
        if not phases:
            return None
        return {"index": self.phase_idx, "count": len(phases), "name": self.phase.name if self.phase else None}

    def _flow_pending(self) -> bool:
        phases = self.template.phases
        if not phases or self.phase_idx >= len(phases):
            return False
        return self.phase_idx >= 0 or any(m.role == "user" for m in self.history)

    def _phase_speakers(self, ph: Phase) -> List[AgentConfig]:
        if ph.speakers == "all":
            return [a for a in self.agents if a.name != self.template.summarizer] or self.agents
        return [a for a in (self.agent(n) for n in ph.speakers) if a]

    # ---------- 广播 ----------

    async def broadcast(self, event: Dict[str, Any]) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def state(self) -> Dict[str, Any]:
        return {
            "type": "state",
            "agents": [a.public() for a in self.agents],
            "templates": [t.public() for t in self.cfg.templates.values()],
            "template": self.template_key,
            "settings": self.settings,
            "schedulers": SCHEDULER_LABELS,
            "search_available": bool(self.searcher),
            "search_label": self.searcher.label if self.searcher else "",
            "history": [m.public() for m in self.history],
            **self._status_fields(),
        }

    def _status_fields(self) -> Dict[str, Any]:
        return {"running": self.running, "speaking": self.speaking, "activity": self.activity, "phase": self.phase_info()}

    async def _status(self) -> None:
        await self.broadcast({"type": "status", **self._status_fields()})

    async def _notice(self, text: str, level: str = "info") -> None:
        await self.broadcast({"type": "notice", "text": text, "level": level})

    async def _system(self, text: str, kind: str = "note") -> None:
        m = Message(speaker=SYSTEM, content=text, role="system", kind=kind)
        self.history.append(m)
        await self.broadcast({"type": "message", "message": m.public()})

    @property
    def running(self) -> bool:
        return self.active

    # ---------- 用户操作 ----------

    async def user_message(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        m = Message(speaker=USER, content=text, role="user")
        self.history.append(m)
        await self.broadcast({"type": "message", "message": m.public()})
        if self.phase:  # 流程进行中被 @ 的人插到本阶段最前面
            for name in reversed(find_mentions(text, self.agents)):
                self.phase_queue.insert(0, name)
        self._kick()

    async def continue_discussion(self) -> None:
        if not any(m.in_context for m in self.history):
            return
        self._kick()

    def _kick(self) -> None:
        """重置发言预算；如果讨论没在进行就启动它。正在讨论时插话，会被下一位发言者看到。"""
        self.turns_left = int(self.settings["max_turns"])
        if self.active:
            self.kicked = True
        else:
            self.active = True
            self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self.task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def clear(self) -> None:
        await self.stop()
        self.history = []
        self._reset_flow()
        await self.broadcast(self.state())

    async def set_template(self, key: str) -> None:
        if key not in self.cfg.templates:
            return
        await self.stop()
        self.template_key = key
        self.history = []
        self._reset_flow()
        await self.broadcast(self.state())

    async def reload(self) -> None:
        try:
            cfg = load_config()
            build_searcher(cfg.search)
        except Exception as e:
            await self._notice(f"配置加载失败，仍使用旧配置：{e}", "error")
            return
        await self.stop()
        self._apply_config(cfg)
        await self.broadcast(self.state())
        await self._notice(f"已重新加载配置：{len(cfg.templates)} 个模板")

    async def update_settings(self, data: Dict[str, Any]) -> None:
        if data.get("scheduler") in SCHEDULER_LABELS:
            self.settings["scheduler"] = data["scheduler"]
        if "max_turns" in data:
            try:
                self.settings["max_turns"] = max(1, min(50, int(data["max_turns"])))
            except (TypeError, ValueError):
                pass
        if "search" in data:
            self.settings["search"] = bool(data["search"]) and bool(self.searcher)
        await self.broadcast({"type": "settings", "settings": self.settings})

    # ---------- 讨论循环 ----------

    async def _run(self) -> None:
        await self._status()
        failures = 0
        cancelled = False
        try:
            while True:
                self.kicked = False
                task = None
                if self._flow_pending():
                    nxt = await self._phase_next()
                    if nxt is None:
                        self.turns_left = 0
                        await self._system("✅ 讨论流程结束。可以继续追问（进入自由讨论），或导出全文。")
                        break
                    agent, task = nxt
                else:
                    if self.turns_left <= 0:
                        break
                    pool = [a for a in self.agents if a.in_free_chat] or self.agents
                    agent = await build_scheduler(self.settings["scheduler"], self.cfg, self.providers, pool).pick(self.history)
                    if agent is None:
                        await self._system("🎤 主持人：这一轮讨论差不多了。可以继续追问，或点「继续讨论」。")
                        break
                    self.turns_left -= 1
                ok = await self._speak(agent, task)
                failures = 0 if ok else failures + 1
                if failures >= 2:
                    await self._system("⚠️ 连续发言失败，讨论已暂停。请检查 API key 和模型配置，然后点「继续讨论」。")
                    self.kicked = False
                    break
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self.active = False
            self.speaking = None
            self.activity = ""
            if self.kicked and not cancelled:  # 收尾期间用户又发了消息
                self.kicked = False
                self.active = True
                self.task = asyncio.create_task(self._run())
            else:
                await self._status()

    async def _phase_next(self) -> Optional[Tuple[AgentConfig, Optional[str]]]:
        """返回 (下一位发言者, 特殊任务)；本阶段说完就进入下一阶段；全部结束返回 None。"""
        phases = self.template.phases
        while True:
            ph = self.phase
            if ph:
                if self.phase_queue:
                    a = self.agent(self.phase_queue.pop(0))
                    if a:
                        return a, None
                    continue
                if self.phase_turns_left > 0:
                    self.phase_turns_left -= 1
                    sched = build_scheduler(self.settings["scheduler"], self.cfg, self.providers, self._phase_speakers(ph), allow_end=False)
                    a = await sched.pick(self.history)
                    if a:
                        return a, None
                    continue
                if self.phase_tail:
                    a = self.agent(self.phase_tail.pop(0))
                    if a:
                        return a, ph.summary_task.strip() or SUMMARY_TASK
                    continue
            self.phase_idx += 1
            if self.phase_idx >= len(phases):
                return None
            await self._enter_phase()

    async def _enter_phase(self) -> None:
        ph = self.phase
        speakers = self._phase_speakers(ph)
        self.phase_queue = [] if ph.turns else [a.name for _ in range(max(1, ph.rounds)) for a in speakers]
        self.phase_turns_left = ph.turns
        self.phase_tail = [self.template.summarizer] if ph.summarize and self.template.summarizer else []
        tags = [t for t, on in (("盲写", ph.blind), ("联网", ph.search and self.settings["search"])) if on]
        title = f"第 {self.phase_idx + 1}/{len(self.template.phases)} 阶段 · {ph.name}" + (f"（{'、'.join(tags)}）" if tags else "")
        await self._system(title + (f"\n{ph.goal.strip()}" if ph.goal else ""), kind="phase")
        await self._status()

    async def _speak(self, agent: AgentConfig, task: Optional[str] = None) -> bool:
        ph = self.phase
        m = Message(speaker=agent.name, role="agent", done=False, phase=self.phase_idx if ph else None)
        self.history.append(m)  # 先占位，保证插话的顺序与界面一致
        self.speaking = agent.name
        await self.broadcast({"type": "message", "message": m.public()})

        parts: List[str] = []
        try:
            search_block = None
            if self.searcher and self.settings["search"] and (ph.search if ph else True) and not task:
                search_block = await self._search_for(agent, m, ph)
            self.activity = "typing"
            await self._status()

            provider = self.providers[agent.provider]
            max_tokens = agent.max_tokens or self.cfg.chat.max_tokens
            msgs = self.build_messages(agent, exclude=m, task=task, search_block=search_block)
            async for delta in provider.stream(agent.model, msgs, agent.temperature, max_tokens, agent.extra_body):
                parts.append(delta)
                await self.broadcast({"type": "delta", "id": m.id, "text": delta})
            m.content = clean_reply("".join(parts), agent.name, [a.name for a in self.agents])
            if not m.content:
                m.error, m.content = True, "（模型返回了空内容）"
        except ProviderError as e:
            m.error, m.content = True, str(e)
        except asyncio.CancelledError:
            m.content = "".join(parts).strip() or "（已停止）"
            m.error = not parts
            raise
        finally:
            m.done = True
            await self.broadcast({"type": "message_done", "message": m.public()})
        return not m.error

    async def _search_for(self, agent: AgentConfig, m: Message, ph: Optional[Phase]) -> Optional[str]:
        """替发言者生成搜索词并搜索，返回要放进提示词的资料；资料同时挂到消息上给网页展示。"""
        self.activity = "searching"
        await self._status()
        planner = self.cfg.search.planner
        if not planner:
            return None
        transcript = "\n".join(f"[{x.speaker}]: {x.content[:300]}" for x in self._visible(agent, m)[-10:])
        queries = await plan_queries(
            self.providers[planner.provider], planner, who=f"{agent.name}（{brief(agent)}）",
            task=(ph.goal.strip() if ph and ph.goal else "在群聊中发言，回应最新的消息"),
            transcript=transcript, today=today(), required=bool(ph),
        )
        if not queries:
            return None
        m.queries = queries
        results, seen = [], set()
        for got in await asyncio.gather(*(self.searcher.search(q) for q in queries), return_exceptions=True):
            if isinstance(got, SearchError):
                await self._notice(str(got), "error")
                continue
            if isinstance(got, BaseException):
                raise got
            for r in got:
                if r.url not in seen:
                    seen.add(r.url)
                    results.append(r)
        results = results[: self.cfg.search.max_results + 2]
        m.sources = [r.public() for r in results]
        await self.broadcast({"type": "message_meta", "id": m.id, "queries": m.queries, "sources": m.sources})
        return format_results(results, queries, today()) if results else None

    def _visible(self, agent: AgentConfig, exclude: Message) -> List[Message]:
        """发言者能看到的上下文：盲写阶段看不到同阶段其他人的发言。"""
        ph = self.phase
        out = []
        for x in self.history:
            if not x.in_context or x is exclude:
                continue
            if ph and ph.blind and x.role == "agent" and x.phase == self.phase_idx and x.speaker != agent.name:
                continue
            out.append(x)
        return out[-self.cfg.chat.context_messages:]

    def build_messages(self, agent: AgentConfig, exclude: Message, task: Optional[str] = None,
                       search_block: Optional[str] = None) -> List[Dict[str, str]]:
        chat, tpl, ph = self.cfg.chat, self.template, self.phase
        max_chars = (ph.max_chars if ph and ph.max_chars else None) or chat.reply_max_chars
        others = "\n".join(f"- {a.name}：{brief(a)}" for a in self.agents if a.name != agent.name)
        system = (
            f"你正在一个多人群聊里，你的名字是「{agent.name}」。今天是 {today()}。\n"
            f"{agent.persona.strip()}\n\n"
            f"群里的其他人：\n- {USER}：提出议题的人\n{others}\n"
        )
        if tpl.guide:
            system += f"\n【讨论形式】{tpl.guide.strip()}\n"
        if ph:
            system += f"\n【当前环节】第 {self.phase_idx + 1}/{len(tpl.phases)} 阶段「{ph.name}」\n本阶段任务：{(task or ph.goal).strip()}\n"
            if ph.blind and not task:
                system += "这是独立作答环节：你看不到其他人本阶段的回答，请完全独立地给出你自己的答案。\n"
        elif task:
            system += f"\n【你的任务】{task}\n"
        system += (
            "\n发言规则：\n"
            "- 直接给出你的观点，可以回应、赞同或反驳其他人，用名字称呼对方。\n"
            f"- 本次发言尽量不超过 {max_chars} 字。\n"
            "- 不要重复别人已经说过的内容，每次都要带来新的信息、角度或质疑。\n"
            "- 想让某位成员回应你时，可以用 @名字 点名。\n"
            "- 只代表你自己说话：开头不要写自己的名字，也不要编造其他人的发言。"
        )
        msgs: List[Dict[str, str]] = [{"role": "system", "content": system}]
        for x in self._visible(agent, exclude):
            if x.speaker == agent.name:
                msgs.append({"role": "assistant", "content": x.content})
            else:
                refs = "；".join(f"[{i}] {s['title']} {s['url']}" for i, s in enumerate(x.sources, 1) if f"[{i}]" in x.content)
                msgs.append({"role": "user", "content": f"[{x.speaker}]: {x.content}" + (f"\n（{x.speaker} 引用的资料：{refs}）" if refs else "")})
        if search_block:
            msgs.append({"role": "user", "content": search_block})

        # 合并连续同角色消息（部分模型要求 user/assistant 交替），并保证首尾都是 user
        merged: List[Dict[str, str]] = msgs[:1]
        for x in msgs[1:]:
            if merged[-1]["role"] == x["role"]:
                merged[-1] = {"role": x["role"], "content": merged[-1]["content"] + "\n\n" + x["content"]}
            else:
                merged.append(x)
        if len(merged) > 1 and merged[1]["role"] == "assistant":
            merged.insert(1, {"role": "user", "content": "（更早的聊天记录已省略）"})
        if merged[-1]["role"] != "user":
            merged.append({"role": "user", "content": "（轮到你继续发言）"})
        return merged

    # ---------- 导出 ----------

    def export_markdown(self) -> str:
        topic = next((m.content for m in self.history if m.role == "user"), "群聊记录")
        lines = [f"# {topic.splitlines()[0][:60]}", "",
                 f"> {self.template.icon} {self.template.name} · 导出于 {datetime.datetime.now():%Y-%m-%d %H:%M}", ""]
        avatars = {a.name: a.avatar for a in self.agents}
        for m in self.history:
            if m.role == "system":
                if m.kind == "phase":
                    head, _, goal = m.content.partition("\n")
                    lines += [f"## {head}", ""] + ([*(f"> {g}" for g in goal.strip().splitlines()), ""] if goal else [])
                else:
                    lines += [f"*{m.content}*", ""]
            elif m.role == "user":
                lines += [f"**🙂 我**：{m.content}", ""]
            elif not m.error:
                lines += [f"### {avatars.get(m.speaker, '🤖')} {m.speaker}", "", m.content, ""]
                if m.sources:
                    lines += ["<details><summary>参考资料</summary>", ""]
                    lines += [f"{i}. [{s['title']}]({s['url']})" for i, s in enumerate(m.sources, 1)]
                    lines += ["", "</details>", ""]
        return "\n".join(lines)


def clean_reply(text: str, me: str, names: List[str]) -> str:
    """去掉模型自己加的名字前缀，并截断它替别人编造的发言。"""
    text = text.strip()
    prefix = re.compile(r"^\s*(?:\[|【)?\s*" + re.escape(me) + r"\s*(?:\]|】)?\s*[:：]\s*")
    text = prefix.sub("", text, count=1)
    if text.startswith(f"【{me}】") or text.startswith(f"[{me}]"):
        text = text[len(me) + 2:].lstrip()
    speakers = [USER] + [n for n in names if n != me]
    fake = re.compile(r"\n\s*(?:\[|【)(?:" + "|".join(re.escape(n) for n in speakers) + r")(?:\]|】)\s*[:：]?")
    hit = fake.search(text)
    if hit:
        text = text[:hit.start()]
    return text.strip()
