"""加载 config.yaml 和 templates/*.yaml，转成带默认值的数据结构。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class AgentConfig:
    name: str
    provider: str
    model: str
    persona: str = ""
    bio: str = ""
    avatar: str = "🤖"
    color: str = "#6b7280"
    temperature: float = 0.8
    max_tokens: Optional[int] = None                          # 不填则用 chat.max_tokens
    extra_body: Dict[str, Any] = field(default_factory=dict)  # 透传给 API 的额外参数
    in_free_chat: bool = True                                 # 是否参与自由讨论（书记员之类的角色可关掉）

    def public(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "avatar": self.avatar,
            "color": self.color,
            "bio": self.bio,
            "model": self.model,
            "provider": self.provider,
        }


@dataclass
class ModelRef:
    """主持人、搜索词生成器这类"幕后"模型。"""
    provider: str
    model: str
    max_tokens: int = 500
    extra_body: Dict[str, Any] = field(default_factory=dict)
    min_turns: Optional[int] = None   # 仅主持人使用：至少讨论几轮之后才能结束


@dataclass
class ChatConfig:
    scheduler: str = "round_robin"
    max_turns: int = 8
    context_messages: int = 40
    reply_max_chars: int = 200
    max_tokens: int = 800
    moderator: Optional[ModelRef] = None


@dataclass
class SearchConfig:
    provider: str = "none"            # duckduckgo | tavily | bocha | none
    api_key: str = ""
    api_key_env: str = ""
    max_results: int = 5
    enabled: bool = True              # 网页上联网开关的默认值
    planner: Optional[ModelRef] = None  # 生成搜索词的模型，不填则用主持人的模型


@dataclass
class Phase:
    name: str
    goal: str = ""                    # 本阶段任务，写进发言者的提示词
    speakers: Any = "all"             # all | 名字列表
    rounds: int = 1                   # 按顺序每人发言几轮
    turns: int = 0                    # >0 时改为由调度器（如 AI 主持人）选人，共发言 turns 次
    blind: bool = False               # 盲写：看不到本阶段其他人的发言
    search: bool = False              # 发言前联网搜索
    summarize: bool = False           # 阶段结束时由模板的 summarizer 小结
    summary_task: str = ""            # 小结的具体要求，不填则用通用的阶段小结
    max_chars: Optional[int] = None   # 本阶段建议字数


@dataclass
class Template:
    key: str
    name: str
    icon: str = "💬"
    description: str = ""
    guide: str = ""                   # 整场讨论的总体说明，写进每位发言者的提示词
    placeholder: str = ""             # 输入框提示
    examples: List[str] = field(default_factory=list)  # 空状态里的示例议题，点击即可填入
    summarizer: Optional[str] = None  # 负责阶段小结的角色名
    agents: List[AgentConfig] = field(default_factory=list)  # 为空则使用 config.yaml 里的成员
    phases: List[Phase] = field(default_factory=list)        # 为空就是自由群聊

    def public(self) -> Dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "icon": self.icon, "description": self.description,
            "placeholder": self.placeholder, "phases": [p.name for p in self.phases], "examples": self.examples,
        }


FREE_TEMPLATE_KEY = "free"


@dataclass
class AppConfig:
    providers: Dict[str, Dict[str, Any]]
    chat: ChatConfig
    agents: List[AgentConfig] = field(default_factory=list)
    search: SearchConfig = field(default_factory=SearchConfig)
    templates: Dict[str, Template] = field(default_factory=dict)


def config_path() -> Path:
    p = Path(os.environ.get("GROUPCHAT_CONFIG", "config.yaml"))
    return p if p.is_absolute() else ROOT / p


def _resolve(raw: Dict[str, Any], models: Dict[str, Dict[str, Any]], where: str) -> Dict[str, Any]:
    """展开 use: 模型别名。别名里的字段作为默认值，条目自己写的字段优先。"""
    raw = dict(raw)
    alias = raw.pop("use", None)
    if alias is None:
        return raw
    if alias not in models:
        raise ValueError(f"{where} 使用了未定义的模型别名: {alias}")
    return {**models[alias], **raw}


def _model_ref(raw: Optional[Dict[str, Any]], models, providers, where: str) -> Optional[ModelRef]:
    if not raw:
        return None
    fields = ModelRef.__dataclass_fields__
    ref = ModelRef(**{k: v for k, v in _resolve(raw, models, where).items() if k in fields})
    if ref.provider not in providers:
        raise ValueError(f"{where} 使用了未定义的 provider: {ref.provider}")
    return ref


def _agents(raws: List[Dict[str, Any]], models, providers, where: str) -> List[AgentConfig]:
    agents = [AgentConfig(**_resolve(a, models, f"{where}成员「{a.get('name')}」")) for a in raws or []]
    names = [a.name for a in agents]
    if len(names) != len(set(names)):
        raise ValueError(f"{where}里有重名成员")
    for a in agents:
        if a.provider not in providers:
            raise ValueError(f"{where}成员「{a.name}」使用了未定义的 provider: {a.provider}")
    return agents


def _load_template(path: Path, models, providers) -> Template:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    where = f"模板 {path.name} "
    phases = [Phase(**p) for p in raw.pop("phases", None) or []]
    agents = _agents(raw.pop("agents", None) or [], models, providers, where)
    t = Template(key=path.stem, agents=agents, phases=phases, **raw)
    names = {a.name for a in agents}
    for p in phases:
        if isinstance(p.speakers, list) and agents:
            missing = [n for n in p.speakers if n not in names]
            if missing:
                raise ValueError(f"{where}阶段「{p.name}」里的发言者不存在: {missing}")
    if t.summarizer and agents and t.summarizer not in names:
        raise ValueError(f"{where}的 summarizer「{t.summarizer}」不在成员里")
    return t


def load_config() -> AppConfig:
    raw = yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
    providers = raw.get("providers") or {}
    models = raw.get("models") or {}

    chat_raw = dict(raw.get("chat") or {})
    mod_raw = chat_raw.pop("moderator", None)
    chat = ChatConfig(**chat_raw)
    chat.moderator = _model_ref(mod_raw, models, providers, "主持人")

    search_raw = dict(raw.get("search") or {})
    planner_raw = search_raw.pop("planner", None)
    search = SearchConfig(**search_raw)
    search.planner = _model_ref(planner_raw, models, providers, "搜索词生成器") or chat.moderator

    agents = _agents(raw.get("agents") or [], models, providers, "config.yaml ")

    templates = {FREE_TEMPLATE_KEY: Template(
        key=FREE_TEMPLATE_KEY, name="自由群聊", icon="💬",
        description="成员自由讨论，由发言方式决定谁接话。", placeholder="说点什么…",
        examples=["年轻人该不该辞职去大理当数字游民？", "AI 会让程序员失业吗？", "十年后最值钱的技能是什么？"],
    )}
    tdir = ROOT / "templates"
    for path in sorted(tdir.glob("*.yaml")) if tdir.is_dir() else []:
        t = _load_template(path, models, providers)
        templates[t.key] = t

    return AppConfig(providers=providers, chat=chat, agents=agents, search=search, templates=templates)
