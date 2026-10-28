# AI 群聊

把多个不同人设、不同底层模型的 AI 放进一个群。你提一个问题，它们会互相讨论、引用、反驳；讨论中你可以随时插话，也可以用 `@名字` 点名某位成员回应。

除了自由聊天，还支持按**讨论模板**分阶段推进，比如「分阶段研讨」「头脑风暴」，并且可以让 AI 发言前**联网搜索**，引用处标注来源。

## 功能

- **多模型、多人设**：每个成员单独配置模型和人设；任何兼容 OpenAI 接口的服务都能接（DeepSeek、OpenRouter、Kimi、通义、本地 Ollama、各种中转站）。
- **三种发言方式**：轮流、随机、AI 主持人决定谁说和何时结束。主持人会避免两个人一直来回对话，让每个人都有机会发言。
- **讨论模板**：把讨论拆成若干阶段，每个阶段可以设置任务说明、发言者、轮数、盲写（看不到别人的回答）、联网、书记员小结。
  - 🎓 **分阶段研讨**：澄清概念 → 摆证据 → 机制交锋 → 反例与边界 → 结论。适合分析型议题。
  - 💡 **头脑风暴**：盲写 → 互评 → 投票 → 深化 → 出大纲。适合找联想、找选题。
- **联网搜索**：发言前由一个便宜的模型生成搜索关键词，把搜索结果作为资料交给发言者；回复里的 `[1]` 可以点开看原文。
- **随时插话、@ 点名、停止 / 继续**。
- **导出 Markdown**：整场讨论连同参考资料一起导出，可以直接当文章素材。
- **局域网访问**：手机、平板打开同一个网址就能一起围观。

## 快速开始

需要 Python 3.9 及以上。

```bash
cp .env.example .env      # 填入你要用的 API key
./run.sh                  # 首次运行会自动创建虚拟环境并安装依赖
```

然后打开 http://127.0.0.1:8770 。

不想花钱、只想先看看界面（全部使用假模型，不需要 key）：

```bash
GROUPCHAT_CONFIG=config.demo.yaml ./run.sh
```

让同一局域网的其他设备也能访问：

```bash
HOST=0.0.0.0 ./run.sh     # 然后用本机的局域网 IP 访问，例如 http://192.168.x.x:8770
```

服务没有访问密码，局域网里任何人都能使用，花的是你的 API 额度。

## 配置

所有设置都在 [config.yaml](config.yaml) 里，改完后在网页上点「重新加载配置」即可生效，不用重启。

| 配置段 | 作用 |
|---|---|
| `providers` | 模型服务：地址和 API key 对应的环境变量名 |
| `models` | 模型别名，例如 `deepseek-pro`。成员用 `use: 别名` 引用，换模型只改一处 |
| `search` | 联网搜索服务：`duckduckgo`（免费，无需 key）、`tavily`、`bocha` 或 `none` |
| `chat` | 发言方式、每轮最多发言次数、上下文长度、AI 主持人用的模型 |
| `agents` | 自由群聊的成员：名字、头像、颜色、人设、模型 |

推理模型（例如 DeepSeek V4 Pro）会先思考再回答，思考也会占用 token。如果回复经常为空，可以调大该成员的 `max_tokens`，或者用 `extra_body: {thinking: {type: disabled}}` 关闭思考。

### 自定义讨论模板

模板放在 [templates/](templates/) 目录里，一个 YAML 文件就是一个模板，文件名就是模板的 key。复制一份现有模板改一改，再点「重新加载配置」就能用。

```yaml
name: 分阶段研讨
summarizer: 书记员              # 负责阶段小结的角色
agents: [...]                   # 本模板的角色；不写则使用 config.yaml 里的成员
phases:
  - name: 澄清概念
    goal: 厘清议题中关键概念的原始含义…   # 写进发言者的提示词
    speakers: [史料派, 理论家]   # 或 all
    rounds: 1                    # 按顺序每人说几轮
    search: true                 # 发言前联网
  - name: 机制交锋
    speakers: all
    turns: 5                     # 改由 AI 主持人选人，共 5 次发言
    summarize: true              # 阶段结束时由书记员小结
  - name: 盲写
    blind: true                  # 看不到本阶段其他人的发言
```

## 代码结构

```
app/
  config.py      读取 config.yaml 和 templates/
  providers.py   模型接口（新服务类型：实现 stream / complete，注册到 PROVIDER_TYPES）
  schedulers.py  发言调度（新策略：继承 Scheduler 实现 choose()，注册到 SCHEDULERS）
  search.py      联网搜索（新搜索服务：实现 _search()，注册到 SEARCH_TYPES）
  engine.py      群聊房间：模板阶段推进、提示词构造、WebSocket 广播、导出
  main.py        FastAPI 入口
static/          网页前端（原生 JS，不需要构建）
templates/       讨论模板
```

## 目前的限制

- 聊天记录只保存在内存里：刷新网页不会丢，重启服务会丢。需要保存的讨论请先导出。
- 只有一个群，所有打开网页的人看到的是同一个群。

后续计划见 [TODO.md](TODO.md)。
