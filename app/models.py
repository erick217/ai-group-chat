from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

USER = "用户"


@dataclass
class Message:
    speaker: str             # 发言者名字；用户固定为 USER，系统提示为 "系统"
    content: str = ""
    role: str = "agent"      # user | agent | system
    done: bool = True        # 流式生成中为 False
    error: bool = False      # 生成失败的消息不进入上下文
    kind: str = ""           # 系统消息的类型：phase（阶段分隔）| note
    phase: Optional[int] = None  # 发言时所处的阶段序号
    queries: List[str] = field(default_factory=list)          # 发言前联网搜索的关键词
    sources: List[Dict[str, str]] = field(default_factory=list)  # 搜索到的资料 {title, url, snippet}
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)

    @property
    def in_context(self) -> bool:
        return self.role in ("user", "agent") and self.done and not self.error and bool(self.content.strip())

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id, "speaker": self.speaker, "content": self.content, "role": self.role,
            "done": self.done, "error": self.error, "ts": self.ts, "kind": self.kind, "phase": self.phase,
            "queries": self.queries, "sources": self.sources,
        }
