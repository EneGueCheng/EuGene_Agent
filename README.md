# SJTUClaw

基于 LLM API 调用的本地 AI 助手，支持 CLI 与 Web 图形界面，具备多轮对话、会话管理、长期记忆、上下文压缩、工具调用、全文搜索、定时日程调度、沙箱安全、主题系统等能力。

## 运行环境

| 项目 | 说明 |
|------|------|
| **开发语言** | Python 3.10+ |
| **操作系统** | Windows / macOS / Linux 均可 |
| **架构** | 前后端一体，后端 Flask + 前端原生 HTML/JS/CSS（SPA） |
| **LLM** | 通过 OpenAI 兼容 API 调用（支持 DeepSeek、GLM 等） |
| **网络** | 需能访问 LLM API 地址（如 `models.sjtu.edu.cn`） |
| **存储** | 纯本地 JSON 文件，无需数据库 |
| **依赖安装** | `pip install -r requirements.txt` |

## 依赖说明

### 核心依赖（`requirements.txt`，必须安装）

| 包名 | 用途 |
|------|------|
| `requests>=2.28.0` | HTTP 客户端，调用 LLM API |
| `python-dotenv>=1.0.0` | 从 `.env` 文件加载环境变量 |
| `flask>=3.0.0` | Web 服务框架，支撑 Gateway 模式 |
| `tavily-python>=0.7.0` | Tavily 网络搜索 + 内容提取（有 API Key 优先） |
| `ddgs>=6.0` | DuckDuckGo 网络搜索（免配置，自动回退） |

```bash
pip install -r requirements.txt
```

### 可选依赖

| 包名 | 用途 | 安装命令 |
|------|------|----------|
| `pywebview>=6.0` | Desktop App 原生窗口模式 | `pip install pywebview` |
| `chardet` | 文件编码自动检测（降级方案，不装也能用） | `pip install chardet` |
| `duckduckgo_search` | ddgs 不可用时的二次回退（不装也能用） | `pip install duckduckgo_search` |
| `pyinstaller` | 打包为 EXE | `pip install pyinstaller` |

### 开发工具（`requirements-dev.txt`，可选）

| 包名 | 用途 |
|------|------|
| `ruff>=0.16.0` | Python 代码检查与格式化 |
| `pre-commit>=4.0.0` | 提交前自动检查钩子 |

```bash
pip install -r requirements-dev.txt
pre-commit install
```

## 快速开始

```bash
# CLI 模式（默认）
python -m src.main

# Gateway 模式（Web 服务）
python -m src.main --gateway          # 默认端口 8080
python -m src.main --gateway --port 9000

# Desktop App 模式（pywebview 原生窗口）
python -m src.main --app
pip install pywebview && python -m src.main --app   # 首次使用安装依赖

# Tube 模式（管道式，stdin → agent → stdout）
echo "What is 2+2?" | python -m src.main --tube
python -m src.main --tube --workspace ./myproject --prompt "review this code"
python -m src.main --tube --no-tools < input.txt

# 打包为 EXE（双击默认进入 Desktop App 模式）
pyinstaller sjtuclaw.spec
.\dist\sjtuclaw.exe            # 双击或直接运行
```

## 配置

复制 `.env.example` 为 `.env` 并填写必要配置：

```bash
cp .env.example .env
```

### 必填变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `LLM_API_KEY` | LLM API 密钥 | `sk-xxxx` |
| `LLM_BASE_URL` | LLM API 地址（OpenAI 兼容接口） | `https://models.sjtu.edu.cn/api/v1` |
| `LLM_MODEL` | 模型调用名 | `deepseek-chat` |

### 可选变量（有默认值）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_COMPACTION_THRESHOLD` | 消息数超过此值触发上下文压缩 | `30` |
| `LLM_COMPACTION_TOKEN_THRESHOLD` | Token 数超过此值也触发压缩（0=禁用） | `8000` |
| `LLM_COMPACTION_KEEP_RECENT` | 压缩后保留的最近消息数 | `20` |
| `LLM_SKILL_SELECTION_MODE` | Skill 选择模式：`auto` / `manual` / `all` | `auto` |
| `MINERU_API_BASE_URL` | MinerU 文档解析 API 地址 | `https://mineru.net/api/v1/agent` |
| `TAVILY_API_KEY` | Tavily 网络搜索 API Key（可选。不配自动回退 DuckDuckGo） | — |

---

## Step 0: 环境准备与 LLM API 接入

- `core/config.py` — 从 `.env` 加载配置，含模型映射表
- `core/client.py` — OpenAI 兼容 `/chat/completions` 调用，含同步 `chat()` 和流式 `chat_stream()`
- `core/errors.py` — 异常类型：`ConfigMissingError`、`ApiRequestError`、`ApiResponseError`、`ApiConnectionError`
- `core/model_store.py` — 多模型 CRUD + 连接测试，API Key base64 存储

---

## Step 1: 基础多轮对话

终端持续输入的多轮对话 Loop，每轮将完整历史 messages 发送给 LLM。

- `session/model.py` — `Session` 类：维护 `_messages` 列表，提供 `add_user()`、`add_assistant()`
- `cli.py` — `run_loop()` 编排 I/O、session、client 的协作，`/exit` 退出

---

## Step 2: 多 Session 管理与持久化

支持创建、切换、恢复多个独立 session，程序重启后历史不丢失。每个 session 保存为 `.claw/sessions/{id}.json`。

- `session/store.py` — `SessionStore`：JSON 文件读写，`load_all()`、`save()`、`delete()`、`generate_id()`、`search()`

### CLI Session 命令

| 命令 | 行为 |
|------|------|
| `/session new` | 创建新 session 并切换 |
| `/session list` | 列出所有 session |
| `/session switch <id>` | 切换到指定 session |
| `/session view <id>` | 查看完整对话历史 |
| `/session rename <id> <title>` | 重命名 |
| `/session delete <id>` | 删除 session |

---

## Step 3: System Prompt、Soul 与 Memory

上下文分层：
1. **system prompt** — 系统规则（从文件加载）
2. **soul** — 身份风格（从文件加载）
3. **memory** — 跨 session 长期记忆
4. **session messages** — 当前会话历史

- `memory/store.py` — `MemoryStore`：JSON 持久化到 `.claw/memory/memories.json`
- `memory/auto_extract.py` — 自动从对话中提取长期记忆
- `context/builder.py` — `build()` 纯函数：组装八层上下文

### CLI Memory 命令

| 命令 | 行为 |
|------|------|
| `/memory add <content>` | 添加记忆 |
| `/memory list` | 列出所有记忆 |
| `/memory delete <id>` | 删除记忆 |

---

## Step 4: 上下文压缩 Compaction

长对话自动压缩旧消息为摘要 + 关键事实，保留最近 N 轮原文（默认阈值 30 条 / Token 8000，保留 20 条，可通过 `.env` 配置）。

- `context/compactor.py` — `compact()`：混合压缩，一次 LLM 调用产出 `summary` + `key_facts`
- `context/token_utils.py` — Token 估算工具，中文 2 字符/token、英文 4 字符/token 的保守估算
- `Session.needs_compaction()` — 双阈值判断（消息数 + Token 数），任一触发即压缩
- `key_facts` 合并去重（最多 10 条），增量保留跨轮压缩结果
- **同步执行**：compaction 在请求线程中同步完成，不再使用 daemon 线程，确保写入磁盘
- **doc 消息纳入压缩**：`role:doc` 附件消息参与压缩（被裁剪后从 `_messages` 移除），其 `file_id` 记录到 `compacted_file_ids`，后续构造上下文时排除这些历史附件，避免重复加载过期文件
- **失败保护**：压缩失败时保留旧数据和旧 facts，回滚 session 状态
- **结构化 Markdown 摘要**：summary 按章节输出（当前任务 / 已完成 / 约束与偏好 / 待解决 / 下一步），fallback 到纯文本
- **摘要漂移检测**：每次压缩后使用 `difflib.SequenceMatcher` 比较新旧摘要相似度，< 30% 时打日志告警
- **Gateway 结果展示**：非流式响应返回 `compacted` + `compaction_summary` 字段

### CLI Compaction 命令

| 命令 | 行为 |
|------|------|
| `/compact` | 手动触发上下文压缩并展示结果 |

### 压缩后的上下文结构（八层）

```
layer 1: system prompt
layer 2: soul + 用户偏好
layer 3: skill prompts          ← 已启用 skill 的 prompt
layer 4: tool definitions       ← API tools 参数，非 messages
layer 5: memory                 ← 跨 session 长期记忆
layer 6: session summary        ← 压缩产物（结构化 Markdown）
layer 7: key facts              ← 关键事实（标签式，最多 10 条）
layer 8: recent messages        ← 仅最近 N 条（含 doc→system 展开）
```

---

## Step 5: Tool 体系与 Agent Loop

`reason → act → observe → reason` 外部反馈闭环。使用 **OpenAI 原生 Function Calling**（`tools` 参数）。

### 已实现 Tool（16 个）

#### 只读工具（7 个，不需要审批）

| Tool | 能力 | 限制 |
|------|------|------|
| `current_time` | 返回当前系统时间 | — |
| `list_dir` | 列出目录内容 | 最多 100 项 |
| `read_file` | 读取文本文件 | 最多 10000 字符 |
| `sqlite_query` | 在 workspace 内的 SQLite 数据库上执行 SELECT 查询 | 仅 SELECT，最多 1000 行 / 50 列，Markdown 表格输出 |
| `git` | Git 只读操作：status / log / diff / diff_staged / branch / remote | 要求 workspace 是 Git 仓库 |
| `download` | 生成 workspace 文件的下载链接 | 30 分钟 TTL，免审批 |
| `ocr` | 图片 OCR 文字识别（通过 MinerU Agent API） | 支持 png/jpg/gif/bmp/webp，需 MINERU_API_BASE_URL |

#### 写入工具（9 个，需审批）

| Tool | 能力 | 限制 |
|------|------|------|
| `create_file` | 创建新文本文件 | 路径不能已存在 |
| `overwrite_file` | 覆盖已有文件 | 路径必须已存在 |
| `edit_file` | 按 old_str → new_str 精确替换 | 需唯一匹配 |
| `web_search` | 搜索互联网，返回标题、URL 和摘要 | Tavily（有 Key 优先）/ DuckDuckGo（自动回退） |
| `web_extract` | 使用 Tavily Extract API 从 URL 提取网页正文（清洗后纯文本） | 需 TAVILY_API_KEY，最多 5 个 URL，单篇 8000 字符截断 |
| `git_write` | Git 写入操作：add / commit / push / pull | push 强制 `--no-force` 锁定 |
| `http_request` | 向公网 HTTPS URL 发起 GET/POST 请求 | 拒内网 IP，15s 超时，50KB 截断，仅 text/json/html |
| `shell` | 执行 shell 命令（持久 Shell + 直接执行） | 超时 60s，输出截断 8000 字符，沙箱拦截 |
| `copy_attachment` | 将上传附件拷贝到 workspace | 打通附件→workspace→Shell 处理链路 |

### Agent Loop 流程

```
用户输入 → build() 构造上下文 → chat(..., tools=...) 原生 Function Calling
  ├── content（无 tool_calls）→ session.add_assistant() → 完成
  └── tool_calls → _handle_tool_calls()
        ├── 只读工具 → 直接执行 → tool_result 注入 session → 再次 callLLM
        └── 写入工具 → 创建审批 → 等待用户决策 → 执行/拒绝 → 再次 callLLM
```

### 审批系统（Safety Layer）

写入工具执行前需用户审批，防止 AI 越权操作。审批独立于 session 存储（`.claw/approvals/`），支持 Web 前端实时弹窗。

| 模式 | 行为 |
|------|------|
| `fine`（精细） | 每个写入工具弹出审批窗口，120 秒超时自动拒绝 |
| `auto`（自动） | 跳过审批，直接执行所有工具 |

**审批状态**：`pending` → `approved` / `approved_sandboxed` / `rejected`

**文件**：
- `tools/base.py` — `ToolDef` + `ToolResult` 数据结构
- `tools/registry.py` — `ToolRegistry`：注册、校验、执行、`format_for_api()`
- `tools/__init__.py` — `build_tool_registry()` 工厂函数
- `tools/parser.py` — tool call 协议解析
- `approval/model.py` — `Approval` 数据模型
- `approval/store.py` — `ApprovalStore`：基于 JSON 文件的审批持久化
- `agent.py` — `run_agent_turn()`（同步）+ `run_agent_turn_stream()`（流式），含 `_handle_tool_calls()` / `_resume_resolved_approvals()`

---

## Step 6: Gateway 与 Web 界面

### 三层架构

```
Web 浏览器 → Gateway Server (Flask) → Agent Runtime (run_agent_turn)
```

CLI 与 Gateway 调用同一 `run_agent_turn()`，共享 session store、context builder、tool registry、memory、compaction。

### Session 删除级联清理

删除 session（含 default 清理）时自动级联清理关联数据：

1. **审批记录** — 删除该 session 下的所有 pending/approved/rejected 审批
2. **附件目录** — 使用 `shutil.rmtree` 彻底递归删除 `data/attachments/<session_id>/`
3. **定时任务** — 删除该 session 关联的所有 Scheduler 任务
4. **消息数据** — 非 default 直接删除 `.json` 文件；default 仅清空消息和摘要

### 附件孤儿清理

- **启动自检**：Gateway 启动时扫描 `data/attachments/`，自动删除所有未对应已有 session 的孤儿目录
- **手动清理 API**：`POST /api/attachments/cleanup-orphans` — 前端可主动调用清理孤儿附件
- **UTF-8 编码加固**：`before_request` 强制 JSON 请求 UTF-8 解码，`after_request` 确保响应声明 `charset=utf-8`，防止 Windows 环境下中文乱码

### Gateway 架构

```
gateway/
├── server.py              — Flask HTTP 服务（78 个 API 端点）
├── attachment_store.py    — 附件上传/存储/检索（含 MinerU 缓存管理）
├── theme_compiler.py      — DESIGN.md → CSS 变量编译
├── theme_registry.py      — 主题注册/加载/激活
└── templates/
    └── index.html         — Web 单页应用
```

### Web 界面功能

- 左侧导航文件夹：📁 对话 / 📁 日程 / 📁 文件浏览
- 会话管理：创建、切换、半胶囊按钮（重命名 + 删除）
- 双模式 FileBrowser：LLM 审批模式自动切换只读/写入，侧边栏手动模式自由导航
- 双模式 Shell 终端：manual 自由输入 + llm 命令锁定
- Markdown 渲染（marked + DOMPurify + highlight.js）
- 流式输出开关 + 长对话自动切换
- 模型选择、附件管理面板、记忆管理、消息删除
- 全文搜索框：防抖查询、按 session 分组、匹配词高亮、键盘导航
- 设置页面 7 个 Tab：用户/记忆/模型/通用/数据/沙箱/关于

---

## Step 7: Scheduler 定时日程

### 架构

```
Scheduler (后台线程) ──┐
                       ├──→ Agent Runtime (run_agent_turn)
Gateway (HTTP) ────────┘
```

Scheduler 作为 Gateway 进程内 daemon 线程运行，每 10 秒扫描到期日程。

### 日程类型

| 类型 | schedule_type | recurrence 示例 |
|------|--------------|----------------|
| 一次性 | `once` | — |
| 每天 | `recurring` | `{"type": "daily", "time": "09:00"}` |
| 每小时 | `recurring` | `{"type": "hourly", "minute": 30}` |
| 间隔 | `recurring` | `{"type": "interval", "minutes": 120}` |

### CLI Task 命令

| 命令 | 行为 |
|------|------|
| `/task create <内容> --at <ISO>` | 创建一次性日程 |
| `/task create <内容> --daily 09:00` | 每天定时日程 |
| `/task list` | 列出所有日程 |
| `/task switch <taskId>` | 切换到日程所在 session |
| `/task modify <taskId> --content/--at/--daily ...` | 修改日程 |
| `/task cancel <taskId>` | 删除日程 |
| `/task pause <taskId>` | 暂停周期性日程 |
| `/task resume <taskId>` | 恢复暂停的周期性日程 |

### SSE 实时推送

- **端点**：`GET /api/events` — 每个浏览器连接独占一个队列，断开时自动清理
- **事件类型**：`task_executed`（刷新日程列表）、`new_session_msg`（自动加载消息/未读红点）

---

## Step 8: 设置页面

### 设置 Tab（7 个）

| Tab | 功能 |
|-----|------|
| 👤 用户管理 | 昵称、头像（6 种 Emoji）、简介、语气倾向、回答长度、自动记忆提取开关、Skill 选择模式 |
| 💭 记忆管理 | 查看/添加/删除长期记忆 |
| 🤖 模型管理 | 多模型 CRUD、连接测试、切换激活模型、API Key base64 掩码 |
| ⚡ 通用设置 | 语言切换（zh/en）、明暗主题模式、DESIGN.md 上传注册主题 |
| 📦 数据管理 | 一键导出 JSON 备份、导入恢复、出厂重置 |
| 🛡️ 沙箱安全 | 沙箱总开关、文件系统隔离、网络策略、危险命令拦截、超时/输出限制 |
| ℹ️ 关于 | 版本信息、项目描述 |

### 用户偏好注入 SOUL

设置字段按需注入 LLM 上下文（nickname/bio/tone/answer_length），与 soul.txt 合并为 layer 2 系统消息。

---

## Step 9: Skill System

### 架构

```
Skill 定义（Markdown + YAML frontmatter）→ SkillRegistry → Agent 上下文注入
                                                     ↓
                                          ask_model_which_skills()（AI 自主选择）
```

### Skill 数据模型

- **定义文件**：Markdown 源文件，YAML frontmatter 定义 `id/name/description/tool_defs[]/sandbox_policy`，正文为 prompt
- **状态文件**：`state.json` 记录 `enabled`、`global_applied`、`config{}`，与定义分离
- **安装方式**：.md 文件上传 / 目录安装 / .zip 包安装

### Skill 内置列表

| 类型 | Skill ID | 说明 |
|------|----------|------|
| Core | `skill_core_system` | 系统工具分组 |
| Core | `skill_core_file` | 文件工具分组 |
| Core | `skill_core_shell` | Shell 工具分组 |
| Utility | `course-report` | 课程报告生成 |
| Utility | `material-summary` | 材料汇总 |
| Utility | `presentation-outline` | 演示大纲 |

### 模型自主 Skill 选择

`skill/selector.py` — `ask_model_which_skills()`：每轮对话前用轻量 LLM 调用判断需要哪些可选 skill。核心 skill 恒启用不参与选择。支持 auto/manual/all 三种模式，LLM 失败时优雅降级。

### CLI Skill 命令

| 命令 | 行为 |
|------|------|
| `/skill list` | 列出所有已安装 skill |
| `/skill show <id>` | 查看 skill 详情 |
| `/skill usage [id]` | 查看 skill 调用记录 |
| `/skill <name> <task>` | 显式调用指定 skill 执行任务 |

---

## Step 10: Theme System (主题系统)

7 套主题以 `theme.json` 为设计 Token，通过 `theme_compiler.py` 编译为单一 `compiled.css`。

### 主题列表

| 主题 | 风格 |
|------|------|
| Claude | Anthropic 品牌：Copernicus 衬线体 + StyreneB 人文无衬线，暖橙主色 |
| Mistral-AI | 日落渐变（芥末黄→橙→深红），山脉摄影 |
| Ollama | 极简文档风，黑白 pill 按钮 + 等宽字体 |
| OpenCode | 奶油底色 + 全 Monaco 等宽，终端/README 排版 |
| Replicate | 复刻橙，工业风 |
| Slack | 茄紫，多色点缀协作风 |
| Spotify | 深绿暗色，圆角卡片 |

### 主题系统架构

```
DESIGN.md → parse_design_md() → theme.json → compile_css() → compiled.css
                                                    ↓
                                 [data-theme-scheme="xxx"] CSS 自定义属性
```

- **字体别名**（`font_alias.json`）：专有字体（Copernicus/StyreneB 等）→ 免费替代（Inter/JetBrains Mono/Nunito）
- **字体模式**：`en`（英文优先）/ `cn`（中文优先，PingFang SC / Microsoft YaHei）
- **明暗模式**：light / dark / system（跟随 OS）
- **API**：DESIGN.md 上传注册 → 编译 → 应用 / 预览 spec / 删除

---

## Step 11: Search (全文搜索)

### 全链路搜索

```
用户输入 → 300ms 防抖 → GET /api/search?q=<kw>&limit=20
  → SessionStore.search() 全量扫描 user/assistant 消息
  → 按 session 分组返回 → 匹配词高亮 → 键盘导航 → 点击跳转
```

- **后端**：`session/store.py#search()` — 大小写不敏感，按 updated_at 降序，支持 session_id 过滤
- **前端**：侧边栏常驻搜索框，下拉结果面板含 session 标题、消息索引、角色标识、预览 + 匹配高亮
- **CLI**：`/search <keyword>` 命令

---

## Step 12: Sandbox (沙箱安全系统)

### 架构

```
LLM 调用 Shell 工具 → enforcer.check_command()
  ├── 危险命令正则匹配 → 拒绝
  ├── 文件系统策略 → 注入 cd 前缀
  └── 网络策略 → 注入环境变量
```

### 策略配置文件 `.claw/sandbox.json`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | false | 总开关 |
| `filesystem` | string | `"permissive"` | `permissive`（路径检查）/ `strict`（注入 cd） |
| `network` | string | `"full"` | `full` / `none`（注入 NO_PROXY）/ `allowlist` |
| `block_dangerous_cmds` | bool | true | 拦截 `rm -rf /`、`format C:` 等 11 条危险模式 |
| `timeout_seconds` | int | 60 | 命令超时（5-600） |
| `max_output_chars` | int | 8000 | 输出截断上限（100-100000） |

### 沙箱化批准

审批新增 `approved_sandboxed` 状态：用户批准 Shell 命令时可选择"沙箱执行"，即使全局沙箱关闭也强制启用拦截。前端 API `POST /api/approvals/<id>/approve_sandboxed`。

### 文件

- `sandbox/model.py` — `SandboxPolicy` 数据模型
- `sandbox/store.py` — `SandboxStore`：`.claw/sandbox.json` 读写，损坏时回退默认值
- `sandbox/enforcer.py` — `check_command()`：三层检查 + `CheckResult` 返回值

---

## Step 14: CLI Tube 模式

管道式调用，stdin 输入 → agent 一轮 loop → stdout 输出，适合脚本批处理和 CI 嵌入。

### 参数

| 参数 | 值 | 默认 | 说明 |
|------|----|------|------|
| `--tube` | — | (必需) | 启用管道模式 |
| `--workspace` | `<path>` | 当前目录 | 设置工作目录，agent 可在此执行 Shell |
| `--prompt` | `<text>` | — | 前置提示词，拼接在 stdin 内容之前 |
| `--session` | `<id>` | `tube_temp` | 复用已有 session（持久化记忆），需用 `/session list` 获取 |
| `--no-tools` | — | false | 禁用所有工具（纯 LLM 模式） |

### 使用示例

```bash
# 基本问答
echo "What is 2+2?" | python -m src.main --tube

# 代码审查（含 workspace）
echo "review this" | python -m src.main --tube --workspace ./src --prompt "Code review:"

# 纯 LLM 无工具
python -m src.main --tube --no-tools < input.txt

# Session 复用（记住上下文）
echo "My name is Alice." | python -m src.main --tube --session my_bot
echo "What is my name?"   | python -m src.main --tube --session my_bot
```

### 退出码

| 码 | 含义 |
|:--:|------|
| 0 | 成功 |
| 1 | stdin 为空、配置缺失或 API 错误 |
| 2 | 沙箱拦截（保留） |

### 审批策略

Tube 模式无交互界面，所有审批自动放行（`fine` 模式）。沙箱策略从已保存的 `sandbox.json` 读取，依然生效。

### 文件

- `src/main.py` — `_run_tube()` 函数（~70 行），入口参数解析 + `--tube` 分发

---

## Step 15: Desktop App 模式（pywebview 原生窗口）

将 Gateway 的 Flask Web 服务嵌入到一个原生桌面窗口中，无需手动打开浏览器。双击打包后的 EXE 自动进入此模式。

### 架构

```
双击 EXE → main.py 检测 frozen + 无模式参数
  → 默认 --app 模式 → desktop.py
    ├── 后台启动 Flask 服务（daemon 线程）
    ├── 等待服务就绪
    ├── 创建 pywebview 原生窗口（1200×800，可调整大小）
    └── pywebview 不可用时回退到系统浏览器
```

### 三种使用方式

| 方式 | 命令 |
|------|------|
| 源码运行 | `python -m src.main --app` |
| 打包后双击 | `.\dist\sjtuclaw.exe`（自动检测） |
| 自定义端口 | `.\dist\sjtuclaw.exe --port 9000` |

### 回退策略

若 pywebview 未安装或 WebView2 Runtime 缺失，自动打开系统浏览器访问 Gateway 页面，不会崩溃。

### 文件

- `src/desktop.py` — `run_app()` 函数：Flask 启动 → 窗口创建 → 回退处理
- `src/main.py` — `_run_app()` 分发函数 + 双击无参时默认 `--app` 的自动检测
- `sjtuclaw.spec` — PyInstaller 打包配置，含 `desktop`、`webview` 隐式导入

---

## CLI 完整命令参考

| 命令 | 子命令 | 说明 |
|------|--------|------|
| `/exit` | — | 退出程序 |
| `/session` | `new` / `list` / `switch <id>` / `rename <id> <t>` / `delete <id>` / `view <id>` | 会话管理 |
| `/memory` | `add <c>` / `list` / `delete <id>` | 长期记忆管理 |
| `/compact` | — | 手动触发上下文压缩 |
| `/tools` | — | 列出已注册工具 |
| `/search` | `<keyword>` | 全文搜索所有 session |
| `/task` | `create` / `list` / `switch` / `modify` / `cancel` / `pause` / `resume` | 定时日程管理 |
| `/workspace` | `show` / `set <path>` / `clear` | 工作目录管理 |
| `/approval` | `list` / `approve <id>` / `approve_sandboxed <id>` / `reject <id>` | 审批管理 |
| `/skill` | `list` / `show <id>` / `usage [id]` / `<name> <task>` | Skill 管理 |

---

## Gateway API 概览

**80 个端点**，分为以下分组：

| 分组 | 端点数 | 核心路径 |
|------|:------:|------|
| 页面/信息 | 2 | `/`、`/api/info` |
| 会话 | 7 | `/api/sessions` CRUD + skills + messages |
| 对话 | 1 | `POST /api/chat` (stream) |
| 附件 | 8 | `/api/sessions/<id>/attachments` + preview/raw + cleanup-orphans |
| 文件浏览 | 4 | `/api/fs/*`、`/api/filesystem/*` |
| Shell | 1 | `POST /api/shell/execute` |
| 审批 | 6 | `/api/approvals` CRUD + approve_sandboxed |
| 工具执行 | 1 | `POST /api/tools/execute` |
| 记忆 | 5 | `/api/memories` CRUD + batch-delete + import |
| 搜索 | 1 | `GET /api/search?q=` |
| Skill | 9 | `/api/skills` CRUD + install + toggle |
| 日程 | 8 | `/api/tasks` CRUD + pause/resume + history |
| 模型 | 6 | `/api/models/mgmt` CRUD + test + switch |
| 设置 | 2 | `/api/settings` GET/PATCH |
| 沙箱 | 2 | `/api/sandbox` GET/PATCH |
| 数据 | 3 | `/api/data/export` / `import` / `reset` |
| 主题 | 5 | `/api/themes` 列表/上传/编译/预览/删除 |
| 下载 | 1 | `/api/downloads/<id>` |
| Prompt 模板 | 4 | `/api/prompt-templates` CRUD |
| SSE | 1 | `/api/events` |

---

## 工程结构

```
SJTU_CLAW/
├── .claw/                    — 运行时数据（sessions/approvals/memory/sandbox/...）
├── themes/                   — 7 套主题（design.md + theme.json）+ compiled.css
├── data/                     — 附件存储 + MinerU 缓存
├── src/
│   ├── main.py               — 程序入口（CLI/Gateway/Tube/App 四模式）
│   ├── desktop.py            — Desktop App 模式（pywebview 原生窗口）
│   ├── agent.py               — Agent Loop（LLM ⇄ Tools 循环，最大 10 轮）
│   ├── cli.py                 — CLI 交互 + 10 个命令处理器
│   ├── core/                  — 配置、LLM Client、模型管理
│   ├── session/               — Session 模型、持久化、全文搜索
│   ├── memory/                — 跨 session 记忆 + 自动提取
│   ├── context/               — 八层上下文构建 + 混合压缩
│   ├── tools/                 — 工具定义、注册、实现（16 tools，含 sqlite_query / git / git_write / http_request / web_extract / ocr）
│   ├── approval/              — 审批状态机（4 种状态）
│   ├── gateway/               — Flask HTTP + 附件 + 前端 SPA + 主题编译
│   ├── scheduler/             — 定时日程调度（4 种周期类型）
│   ├── workspace/             — 路径解析与安全边界
│   ├── sandbox/               — 沙箱策略、存储、命令检查守卫
│   └── skill/                 — Skill 注册、安装、AI 选择器
├── requirements.txt           — Python 核心依赖
├── requirements-dev.txt       — 开发工具（ruff, pre-commit）
├── pyproject.toml             — 项目配置
├── sjtuclaw.spec              — PyInstaller 打包配置
├── Dockerfile                 — Docker 镜像构建
├── docker-compose.yml         — Docker 编排
├── skill.md                   — Skill 示例
└── .env.example               — 环境变量模板
```

---

## 未来计划

### 新功能特性

| 方向 | 说明 | 优先级 |
|------|------|:------:|
| ~~快捷键体系~~ | ✅ 已完成 |
| ~~Lint + Format (ruff)~~ | ✅ 已完成 |
| ~~Pre-commit 钩子~~ | ✅ 已完成 |
| ~~消息编辑重发~~ | ✅ 已完成 |
| ~~对话导出~~ | ✅ 已完成 |
| ~~附件孤儿清理 + Session 级联删除~~ | ✅ 已完成 |
| ~~Doc 消息纳入压缩~~ | ✅ 已完成 |
| ~~Web Search 工具（Tavily）~~ | ✅ 已完成 |
| ~~UTF-8 编码加固~~ | ✅ 已完成 |
| ~~Git 工具~~ | ✅ 已完成：git（只读）+ git_write（写入，需审批） |
| ~~HTTP 请求工具~~ | ✅ 已完成：`http_request`（HTTPS 限定 + 内网拒止 + 超时/截断） |
| ~~web_search 免配置回退~~ | ✅ 已完成：DuckDuckGo 自动回退（无 Tavily Key 时） |
| ~~Tavily Extract 网页提取~~ | ✅ 已完成：`web_extract`（清洗后纯文本，需审批） |
| ~~Prompt 模板库~~ | ✅ 已完成：6 个内置模板 + CRUD API + 前端下拉快捷填入 |
| ~~SQLite 只读查询~~ | ✅ 已完成：`sqlite_query`（SELECT 限定，1000 行 / 50 列，免审批） |
| ~~OCR 图片文字识别~~ | ✅ 已完成：`ocr`（MinerU Agent API，支持中英文混排，免审批） |
| 对话分支 (Fork) | 从某条消息分叉出新会话，支持探索性对话树 | 中 |
| MCP 协议支持 | 接入 Model Context Protocol，扩展外部工具生态 | 低 |
| 响应缓存 | 相同问题时命中缓存，减少 API 调用（省钱+加速） | 中 |

### AI 行为优化

| 方向 | 说明 | 优先级 |
|------|------|:------:|
| 记忆冲突检测与修正 | 自动检测新旧记忆的矛盾（如"我是李明"→"我叫李华"），通过 LLM 裁决合并/更新/删除，避免矛盾记忆共存 | 中 |

### 工程化与体验优化

| 方向 | 说明 | 优先级 |
|------|------|:------:|
| CI/CD 流水线 | GitHub Actions 自动运行测试 + PyInstaller 打包 | 中 |
| 结构化日志 | 替换 `print()` 为 `logging` 模块，支持日志级别/文件轮转 | 低 |
| 类型注解覆盖 | 为整个项目补充完整类型注解，配合 mypy/pyright 静态检查 | 低 |
| 移动端适配 | 响应式布局，手机浏览器也能正常使用 Gateway | 中 |
| 消息气泡优化 | 连续消息合并、代码块折叠、LaTeX 渲染 | 中 |
