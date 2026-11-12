SJTUClaw 功能要求汇总
SJTUClaw 是一个最小 agent runtime。用户可以通过 CLI 或图形化操作入口与它对话。claw 需要维护会话、构造上下文、调用 LLM，并在需要时读取环境、执行任务、保存结果。

具体功能要求如下：

实现基础 LLM 调用。程序应能从 .env 或环境变量读取配置，使用通用 messages 结构向模型发送请求并打印 assistant 回复；API KEY 不能写死或提交到仓库中，配置缺失、网络失败和响应异常需要有清晰提示。

支持 CLI 多轮对话。用户可以在终端连续输入消息，程序每轮都把当前 session 的历史上下文发送给 LLM，并把 user 输入和 assistant 回复写回会话历史；普通退出、中断和调用失败需要能基本处理。

支持多 session 管理。用户可以创建、列出和切换 session；不同 session 的历史相互隔离，并能持久化到本地文件。/session 等内部命令由 runtime 处理，不应作为普通消息发送给 LLM。

上下文管理。支持 system prompt、soul、memory 和普通 session 历史等上下文的管理；system prompt 与 soul 从独立配置加载，memory 用于保存跨 session 的长期事实或用户偏好，并支持手动添加、查看和删除。

实现长对话压缩。session 历史过长时，claw 应能把较早消息压缩成当前 session 的 summary，并保留最近几轮原始消息；压缩只处理当前 session 的 conversation context，失败时不能丢失原始历史。

实现工具调用：claw 必须实现工具调用以支持模型与环境交互。模型通过明确协议请求 tool call，runtime 真实执行 tool，把结果写回 session，再让模型基于 observation 继续回答。claw 至少提供获取当前时间、列出目录、读取文本文件 3 个只读 tool，以及后续提到的 advanced tool。

提供 Gateway 与图形化操作入口。Gateway 作为外部入口和 agent runtime 之间的服务层；至少实现一个非 CLI 的图形化入口，可选择网页、桌宠、桌面客户端、QQ/微信/飞书机器人等形态。图形化入口至少支持发送消息、展示历史、展示错误、列出/创建/切换 session 和上传或关联附件；图形化入口不能明文暴露 LLM API KEY，附件 metadata 必须按 session 隔离。

支持定时任务。用户可以创建一次性任务和周期性任务，查看任务列表、状态、执行历史，并取消未来触发；任务需要保存内容、计划、下一次触发时间、状态、所属 session 和执行历史，到期后仍应调用已有 agent loop，结果写回对应 session。

支持 workspace、advanced tool 和 approval。用户可以设置当前 workspace 用于 claw 工作的场所和边界；claw 可以在 workspace 内创建或修改文件、启动 shell 运行命令等，并支持 workspace 和外部入口的文件交互（advanced tools）。写文件和运行命令前必须创建 approval，用户批准后才执行，拒绝或执行结果都需要进入 session 历史。

实现 skill system。claw 应能扫描本地 skills/ 目录，列出可用 skill，并在用户显式调用或模型判断需要时加载对应 SKILL.md 及相关资源；至少实现 3 个 skill，其中必须包含 course-report，用于生成课程报告类 Markdown 草稿，并通过 workspace update tool 与 approval 流程保存结果。

复用同一套 runtime。CLI、Gateway、图形化入口、Scheduler、workspace tool 和 skill 都必须复用同一套 session store、context builder、memory、compaction、tool registry 和 agent loop，不能各自绕过底层逻辑单独调用 LLM 或直接修改状态。

更细的实现步骤和要求见后续内容。

Step 0：环境准备与 LLM API 接入
本 Step 的目标是完成 claw 的最小运行闭环：程序能够读取配置，向 LLM 发送一条消息，并在终端打印模型回复。

完成后，你应该能用一个固定命令启动程序，看到一次真实的模型返回。

0.1 实验目标
本 Step 需要完成以下内容：

在 Windows、WSL 或 macOS 上搭建开发环境。

创建 claw 项目的基础工程结构。

配置 LLM API KEY，并确保密钥不会进入 Git。

实现一次最小 LLM API 调用。

对 API KEY 缺失、网络请求失败、HTTP 异常、响应格式异常等常见错误给出清晰提示。

本 Step 只关注一个最小闭环：配置正确、请求发出、响应解析成功、结果打印出来。

0.2 开发环境
推荐使用以下环境之一：

WSL + Ubuntu 22.04 或更新版本
macOS
需要安装的基础工具包括：

Git
Python
本课程推荐使用 Python 实现。课程不强制具体语言，但要求工程结构清晰、模块边界明确。

0.3 工程结构要求
你需要建立一个可以持续扩展的最小项目结构。具体目录和文件名可以自行设计，但至少应体现以下职责边界：

程序入口
配置读取
LLM API 调用
运行数据或日志目录
0.4 API KEY 配置
真实配置可以来自 .env 或系统环境变量。项目中应提供 .env.example，说明需要哪些配置项，例如：

LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL=
.gitignore 中必须包含真实密钥文件和运行产物，例如：

.env
真实 API KEY 只能保存在 .env 或环境变量中。不能把 API KEY 写死在代码里。不能把 .env 提交到 Git。

警告：不要在代码、README、提交记录、Issue、截图或公开仓库中的任何位置暴露 API KEY。公开仓库通常会被自动扫描，一旦 API KEY 泄漏，可能导致账号被盗用、额度被消耗或服务被封禁。

0.5 裸 API 调用要求
本 Step 需要实现一个最小 LLM 调用能力：

输入：一组 messages
输出：assistant 的回复文本
大多数 Chat Completions / Messages API 都使用 messages 作为输入。messages 是一个数组，每一项代表上下文中的一条消息：

[
  {
    "role": "user",
    "content": "你好，请用一句话介绍你自己。"
  }
]
其中 role 表示这句话是谁说的，content 表示消息正文。本 Step 至少需要理解以下角色：

system
  系统指令，用来规定模型的行为边界和回答风格。

user
  用户输入的问题或请求。

assistant
  模型返回的回复。
本 Step 的最小请求可以只包含一条 user message。也可以加入一条简短的 system message，但不是必须。

示例输入：

你好，请用一句话介绍你自己。
示例输出：

你好，我是一个可以帮助你完成编程、写作和学习任务的 AI 助手。
0.6 最小程序行为
程序运行后，应完成以下动作：

读取配置
检查必要配置是否存在
构造一条 user message
调用 LLM API
打印 assistant 回复
启动命令需要固定，例如：

python -m src.main
实际命令可以按你的工程结构调整，但应写在 README 中。

Step 1：实现多轮对话 Loop
在 Step 0 中，我们已经完成了最小 LLM API 调用：程序可以向模型发送一条消息，并打印模型返回的回复。

本 Step 的目标是在此基础上实现一个最小多轮对话 loop，使 claw 能够在终端中持续接收用户输入，并把当前会话中的历史消息一并发送给模型。

完成本 Step 后，你应该能够在终端中和 claw 进行多轮对话，并让 claw 记住当前运行期间前面几轮说过的内容。

1.1 实验目标
本 Step 需要完成以下内容：

实现一个可以持续运行的 CLI 对话程序。

维护当前会话中的历史消息。

每轮用户输入后，将历史消息和本轮输入一起发送给 LLM。

将 assistant 的回复追加到当前会话历史中。

将核心对话逻辑从终端输入输出中拆出，避免 CLI 和模型调用逻辑混在一起。

支持基础退出命令，例如 /exit。

本 Step 的交付物是一个运行期间有效的连续对话程序。

1.2 多轮对话的基本思想
单轮调用时，程序只向模型发送当前用户输入，因此模型无法知道前面说过什么。

多轮对话的关键是：程序在每一轮请求中主动携带当前会话历史，而不是依赖模型自动记住历史。

例如，用户先说：

你好，我叫小明。
随后再问：

我刚才说我叫什么？
程序需要把前面的 user 和 assistant 消息一起发送给模型。只发送最后一句问题是不够的。

1.3 消息与会话
LLM 本身不会保存对话历史。每次调用模型时，程序都要把本轮需要的上下文放进 messages 里一起发送。

历史中的 assistant message 也要继续发给模型。否则模型只能看到用户连续说了什么，看不到自己前面回答过什么，也就无法基于完整对话继续推理。

Session 是程序侧保存这些 messages 的容器。所谓“当前会话”，就是当前这段对话已经积累下来的 messages。

多轮对话的第一原理是：模型每次调用都是一次新的请求。所谓“记住前文”，本质上是程序把之前的 messages 再次发给模型。

例如第二轮请求通常长这样：

[
  {
    "role": "user",
    "content": "你好，我叫小明。"
  },
  {
    "role": "assistant",
    "content": "你好，小明。"
  },
  {
    "role": "user",
    "content": "我刚才说我叫什么？"
  }
]
当前 session 可以先存在内存中。程序退出后，历史消息丢失是可以接受的。

1.4 对话 Loop 要求
程序需要持续读取用户输入，并完成以下逻辑：

启动时创建当前 session
等待用户输入
识别退出命令
将普通用户输入追加到当前 session
调用 LLM
将 assistant 回复追加到当前 session
打印 assistant 回复
继续等待下一轮输入
1.5 最小程序行为
程序运行后，可以进入一个连续对话界面，例如：

$ python -m src.main

claw started. Type /exit to quit.

User> 你好，我叫小明。
Assistant> 你好，小明！很高兴认识你。

User> 我刚才说我叫什么？
Assistant> 你刚才说你叫小明。

User> /exit
bye.
这个例子说明 claw 已经能够把当前会话中的历史消息发送给模型，从而实现基础多轮对话。

Step 2：多 Session 管理与持久化
在 Step 1 中，我们已经实现了一个内存版多轮对话 loop。程序可以在终端中持续接收用户输入，并把当前会话中的历史消息一并发送给 LLM。

但是，Step 1 仍然存在两个问题：程序退出后会话历史会丢失，用户也无法同时维护多个不同主题的会话。

本 Step 的目标是在 Step 1 的基础上实现多 session 管理与 session 持久化，使 claw 能够创建、切换、保存和恢复不同会话。

完成本 Step 后，你应该能够创建多个 session，在不同 session 之间切换，并在程序重启后恢复历史对话。

2.1 实验目标
本 Step 需要完成以下内容：

支持多个 session。

支持创建、列出、切换和查看 session。

将 session 保存到本地文件中，程序重启后可以恢复。

每个 session 拥有独立的历史消息。

本 Step 的交付物是“多个可恢复的独立会话”。

2.2 为什么需要 Session
在 Step 1 中，程序运行期间只有一个内存版 session。所有对话都写在同一个消息列表里。

这种方式适合最小 demo，但不适合真实使用：

程序退出后，历史消息全部丢失。
不同主题的对话混在一起，模型容易拿错上下文。
例如，用户可能希望同时维护多个会话：

session_001：实现 claw 课程项目
session_002：复习算法作业
session_003：整理实习经历
每个 session 都应该有独立的历史消息。切换到某个 session 后，claw 只应该使用该 session 的上下文，而不是把所有对话混在一起。

2.3 Session 数据要求
你需要扩展 Step 1 的 session，使它可以被唯一识别、展示和持久化。

每个 session 至少需要保存：

sessionId
title
messages
createdAt
updatedAt
字段命名和文件组织可以自行设计，但必须满足：

sessionId 可以用于创建、切换和持久化。

title 可以帮助用户识别不同会话。

messages 保存该 session 独立的历史消息。

createdAt 和 updatedAt 能反映创建与更新时间。

不要把所有会话写进同一个文件后再靠人工区分。程序必须能用 sessionId 稳定找到对应会话。

2.4 Session 持久化
Session 需要保存到本地文件中。推荐使用 JSON，因为它易读、易调试，也方便验收。

要求：

程序重启后，历史 session 不能丢失。

切换 session 后，新的 LLM 请求只使用对应 session 的历史消息。

保存失败时，需要输出清晰错误信息。

JSON 文件损坏或解析失败时，不能静默丢失数据。

2.5 多 Session 命令
本 Step 需要支持最基础的 session 管理命令：

/session new
  创建一个新的 session，并切换到该 session。

/session list
  列出所有已有 session，包括 sessionId、title、消息数量和更新时间。

/session switch <sessionId>
  切换到指定 session。

/session rename <sessionId> <title>
  修改指定 session 的标题。

/session delete <sessionId>
  删除指定 session。
注意：这些命令由 CLI 直接处理，不应该作为普通用户消息发送给 LLM。

2.6 Context Builder 雏形
从本 Step 开始，发送给 LLM 的上下文不应由 CLI 或 LLM client 模块临时拼接。你需要引入一个 context builder 模块负责构造模型输入。

本 Step 中，context builder 至少需要完成：

加入系统指令
加入当前 session 的历史 messages
返回发送给 LLM 的 messages
Context builder 的第一原理是：存储结构不一定等于模型输入结构。session 文件里可以有 title、createdAt、updatedAt 等字段，但发给 LLM 的通常只应该是模型能理解的 messages。

具体函数名、模块名和内部实现可以自行设计。关键是职责边界要清楚：session 负责保存历史，context builder 负责把历史组织成 LLM 输入。

2.7 最小程序行为
程序运行后，可以使用 session 命令管理会话：

$ python -m src.main

claw started. Type /exit to quit.
Created session: session_002

User> /session list
Sessions:
* session_002  新会话        messages=0    updated=2026-06-25 20:10
  session_001  算法作业    messages=8    updated=2026-06-25 20:00

User> 你好，这个会话是用来写数据库作业的。
Assistant> 好的，我们可以在这个会话中讨论数据库作业。

User> /session switch session_001
Switched to session: session_001
History:
User> 这个会话是用来写算法作业的。
Assistant> 好的，我们可以继续讨论算法作业。
这个例子说明：

session 可以被创建和切换。

不同 session 拥有不同历史。

程序重启后仍然可以恢复已有 session。

Step 3：System Prompt、Memory 与 Soul
在 Step 2 中，我们已经实现了多 session 管理与持久化。claw 可以创建、切换、保存和恢复不同会话。

但是，此时 claw 的上下文仍然比较简单：主要由系统指令和当前 session 的历史 messages 组成。随着 agent 能力逐渐增强，我们需要把上下文拆得更清楚：哪些是系统规则，哪些是用户长期记忆，哪些是 agent 的稳定风格，哪些是普通会话历史。

本 Step 的目标是引入三类稳定上下文：

system prompt
memory
soul
完成本 Step 后，claw 应该能够从独立文件加载 system prompt 和 soul，支持手动管理 memory，并在每次调用 LLM 时将这些稳定上下文加入 context builder。

3.1 实验目标
本 Step 需要完成以下内容：

将 system prompt 从代码中拆出，改为从配置文件加载。

新增 soul 配置，用于描述 claw 的稳定身份、风格和交互方式。

实现 memory store，用于保存用户长期偏好、长期事实或长期项目背景。

支持基本 memory 命令，例如添加、查看和删除 memory。

将 system prompt、soul、memory 和当前 session messages 统一组装成 LLM 输入。

明确 stable context 的边界：system prompt、memory 和 soul 是稳定上下文，不应该被普通 session 历史覆盖。

本 Step 的交付物是：稳定上下文能从普通对话历史中分离出来，并在每次请求中进入模型输入。

3.2 为什么需要拆分上下文
一个更完整的 agent 上下文通常包含多类信息：

system prompt
  稳定系统规则。

soul
  agent 的稳定身份和交互风格。

memory
  用户长期偏好、长期项目背景或长期事实。

session messages
  当前 session 的普通对话历史。
它们的生命周期和更新方式不同，不能全部混在普通 messages 里。

例如：

system prompt
  不应该被用户随便覆盖，通常由开发者维护。

memory
  应该长期保存，可以跨 session 复用。

soul
  应该稳定存在，用来控制 claw 的整体风格。

session messages
  只表示当前会话历史，不应该承担长期记忆职责。
3.3 System Prompt
system prompt 用来定义 claw 的基本运行规则。它应该偏向“系统约束”和“行为边界”，例如：

基于已有上下文回答用户问题。
不能伪造工具结果或外部执行结果。
如果上下文不足，需要明确说明。
回答应清晰、直接，并优先帮助用户完成当前任务。
要求：

system prompt 不应该写死在 agent loop 中。

system prompt 应从独立配置文件加载。

修改配置文件后，重启程序可以生效。

system prompt 不应该被普通用户消息覆盖。

3.4 Soul
soul 用来描述 claw 的稳定身份、语气和交互风格。它和 system prompt 不同。

system prompt
  更偏系统规则、权限边界和安全约束。

soul
  更偏 agent 的长期风格、人格设定和工作方式。
例如，可以在 soul 中写入一种稳定表达风格：

你说话时保持轻松、简短。
每句话末尾都加一个“喵”。
这样用户问“你是谁？”，claw 可能回答：

我是 claw，一个帮助你完成学习和编程任务的 agent 喵。
Soul 不应该写得过长，也不应该塞入具体任务状态。具体任务状态应该保存在当前 session 中。

要求：

soul 从独立配置文件加载。

soul 每次调用 LLM 时都会进入上下文。

soul 不用于保存某个 session 的临时任务进度。

soul 不应该被普通用户消息覆盖。

3.5 Memory
memory 用于保存跨 session 的长期信息，例如用户偏好、长期项目背景、常用约束等。

Memory 不等于聊天记录。它应该保存长期有效、未来可能反复使用的信息，而不是把所有历史对话都存进去。

适合写入 memory 的例子：

用户正在实现一个名为 claw 的课程 agent 项目。
用户偏好中文回答。
用户希望课程 Lab 文档语言清晰、任务可验收。
不适合写入 memory 的例子：

用户刚才说了一句“你好”。
用户临时让模型解释某个词。
某一轮对话中的普通寒暄。
本 Step 使用手动 memory 管理：只有明确的 memory 命令会修改 memory。

需要支持：

/memory add <content>
/memory list
/memory delete <memoryId>
注意：这些命令由 CLI 直接处理，不应该作为普通用户消息发送给 LLM。

3.6 稳定上下文的边界
本 Step 需要明确一个边界：

system prompt、memory、soul 是 stable context。
session messages 是 conversation context。
二者职责不同：

stable context
  负责长期规则、长期偏好和长期风格。

conversation context
  负责当前 session 中的临时对话历史和任务进度。
因此：

system prompt 不应该由普通对话自动改写。

soul 不应该由普通对话自动改写。

memory 只能通过明确的 memory 命令写入或删除。

session messages 可以随着对话继续增长。

3.7 最小程序行为
程序运行后，可以通过 memory 命令管理长期记忆，并验证其跨 session 持久化能力：

$ python -m src.main

claw started. Type /exit to quit.

User> /memory add 用户正在实现一个名为 claw 的课程 agent 项目。
Added memory: mem_001

User> /session new
Created session: session_002

User> 我现在在做什么项目？
Assistant> 你正在实现一个名为 claw 的课程 agent 项目。
这个例子说明：

1. memory 可以被手动写入，并持久化到磁盘。
2. memory 在不同 session 中都可见。
3. assistant 可以基于 memory 回答问题，而不依赖当前 session 的历史消息。
4. memory 是跨 session 的长期上下文，而不是某个 session 的局部状态。
Step 4：上下文压缩 Compaction
在 Step 3 中，我们已经将 claw 的上下文拆分为两类：

stable context:
  system prompt
  memory
  soul

conversation context:
  当前 session 的历史 messages
随着对话轮数增加，当前 session 的历史 messages 会不断变长。如果每次都把完整历史发送给 LLM，请求会越来越慢，也可能超过模型上下文长度限制。

本 Step 的目标是实现 compaction：当某个 session 的历史消息过长时，将较早的 messages 压缩成一段 summary，只保留最近几轮原始消息。

需要特别注意：compaction 只处理 session messages，不处理 system prompt、memory 和 soul。

完成本 Step 后，claw 应该能够在长对话中自动压缩旧消息，同时保留当前任务状态和重要上下文。

4.1 实验目标
本 Step 需要完成以下内容：

扩展 session 数据结构，增加当前 session 的 summary。

实现 compaction 判断逻辑，判断当前 session 是否需要压缩。

使用 LLM 将旧消息压缩成摘要。

将旧 messages 合并进 session summary。

只保留最近几轮原始 messages。

LLM 上下文应包括：

system prompt
soul
memory
session summary
recent session messages
明确 compaction 边界：system prompt、memory、soul 不参与压缩。
本 Step 的交付物是：长对话不会无限增长，旧消息能被安全压缩成 session summary。

4.2 为什么需要 Compaction
在 Step 1 和 Step 2 中，claw 的多轮对话依赖完整历史消息。这种方式简单直接，但随着对话变长，会出现几个问题：

上下文越来越长，可能超过模型上下文窗口。

每轮请求都携带大量旧消息，调用速度变慢。

API 调用成本增加。

很多旧消息只是寒暄或中间过程，不值得原样保留。

Compaction 的核心思想是：

把较旧的原始 messages 压缩成 summary

只保留最近几轮 messages 的原文。

也就是说，compact 不是简单删除历史，而是把旧历史换成更短的表示：

压缩前：stable context + 很长的 session messages
压缩后：stable context + session summary + recent messages
其中 session summary 由 LLM 根据旧消息生成，负责保留长期任务状态；recent messages 原样保留，负责保留最近几轮对话的细节。

生成 session summary 时，需要使用一个专门的 compact prompt。它会把已有 summary 和要压缩的 old messages 交给 LLM，并要求模型输出新的 summary：

已有 summary + old messages + compact prompt -> new summary
这个 compact prompt 的作用是告诉模型：哪些信息要保留，哪些无关内容可以删除，输出要作为后续对话上下文使用。

压缩后的上下文应该仍然能回答“当前任务是什么、已完成什么、用户有哪些明确要求、下一步要做什么”这类问题。

4.3 Compaction 与 Stable Context 的边界
Step 3 已经引入了三类 stable context：

system prompt
memory
soul
这些内容不应该参与 compaction。

原因如下：

system prompt
  是系统规则和行为边界，应该由开发者维护，不能被摘要模型改写。

memory
  是长期记忆，跨 session 生效，应该由 memory store 单独管理。

soul
  是 claw 的稳定身份和交互风格，应该保持稳定，不应该被普通对话压缩改写。
因此，compaction 只处理：

session.summary
session.messages
不处理：

system prompt 配置
soul 配置
memory store
只压缩当前 session 的对话历史。系统规则、长期记忆和稳定风格不是普通聊天记录，不能交给摘要过程改写。

4.4 Session Summary
本 Step 需要为每个 session 增加 summary。

要求：

summary 属于当前 session，只在当前 session 内生效。

summary 来自较早的 messages，用于保留长期任务状态和重要上下文。

summary 会持久化到该 session 的本地数据中。

summary 不是 memory，不会跨 session 共享。

最近几轮原始 messages 仍然需要保留。它们用于保存最新对话细节，不能全部替换成 summary。

4.5 Compaction 触发策略
Token 是模型处理文本的基本单位，也是上下文窗口限制的来源。精确 token 计算依赖具体模型和 tokenizer；本 Step 可以先使用字符数、消息数量或其他可解释的阈值近似判断。

无论采用哪种策略，都需要满足：

没有超过阈值时，不触发 compaction。

触发后，只压缩较旧的 messages。

最近消息需要原样保留。

新 summary 需要合并已有 summary 和本次被压缩的旧消息。

compaction 完成后，需要保存 session。

触发策略应写在 README 或代码注释中，便于验收时理解。

4.6 Summary 质量要求
Compaction 生成的 summary 应保留：

当前任务
已经完成的内容
用户明确提出的要求、偏好和约束
尚未解决的问题
影响继续回答的重要事实
应删除：

寒暄
重复表达
无关细节
没有继续使用价值的中间过程
summary 不需要固定格式，但必须简洁、可读，并适合作为继续对话的上下文。

4.7 Compaction 流程要求
加入 compaction 后，一轮普通对话需要在合适的位置检查是否应压缩。

要求：

普通 LLM 调用失败时，不追加空 assistant message。

compaction 调用失败时，不删除旧 messages。

summary 为空或无效时，不应用本次 compaction 结果。

compaction 成功后，需要打印压缩结果，并展示更新后的 session summary 或 summary 预览。

保存失败时，需要提示用户当前 compaction 结果可能没有保存成功。

最重要的原则是：

compaction 失败时，不能删除旧 messages。
否则可能导致 session 历史不可恢复地丢失。

4.8 手动 Compaction 命令
可以选做一个手动命令：

/compact
它用于立即压缩当前 session，方便调试和验收。该命令应由 CLI 直接处理，不作为普通用户消息发送给 LLM。

每次 compaction 成功后，无论是自动触发还是手动触发，程序都需要打印本次压缩结果，并展示更新后的 session summary 或 summary 预览。这样用户才能判断摘要是否保留了关键任务状态，而不是只看到 messages 数量变化。

4.9 最小程序行为
程序运行后，可以在长对话中自动触发 compaction：

$ python -m src.main

claw started. Type /exit to quit.

User> 我们继续完善 claw 的 Lab 文档。
Assistant> 好的，我们继续。

...

[system] compact session session_001: old_messages=28, recent_messages=8
[system] summary:
当前任务：完善 claw 的 Lab 文档。
已完成：Step 0 到 Step 3 的核心能力。
下一步：继续完善 Step 4 的 compaction 行为。

User> 我们刚才完成了哪些 Step？
Assistant> 目前已经完成 Step 0 到 Step 3，并正在完善 Step 4：Compaction。
也可以使用手动命令：

User> /compact
Compacted session session_001.
Old messages: 28
Recent messages: 8
Summary updated: yes
Summary:
当前任务：完善 claw 的 Step 4 文档。
已完成：说明 compaction 的边界、触发策略和失败保护。
下一步：验证 context builder 能正确使用 session summary。
Step 5：只读 Tool、外部环境反馈闭环与 Agent Loop
在 Step 4 中，我们已经实现了 compaction，使 claw 能够在长对话中压缩旧消息，并通过：

system prompt
soul
memory
session summary
recent session messages
构造可持续增长的上下文。

但是，到目前为止，claw 仍然只能基于已有上下文和模型自身知识回答问题。它还不能主动读取外部环境，例如当前时间、项目目录或文件内容。

本 Step 的目标是为 claw 加入只读 tool，使它能够读取外部环境信息，并将 tool 执行结果反馈回 agent loop。这样，claw 就从“只能聊天的模型调用程序”，升级为“能够观察外部环境的最小 agent”。

需要特别注意：本 Step 只实现只读 tool，不实现写文件、改文件、删除文件、执行危险命令等 update tool。

完成本 Step 后，claw 应该能够根据用户问题主动调用只读 tool，并基于真实 tool result 给出最终回答。

5.1 实验目标
本 Step 需要完成以下内容：

定义统一的 Tool 数据结构。

实现 tool registry，用于注册、查找和执行 tool。

实现至少 3 个只读 tool。

定义模型发起 tool call 的协议。

将 agent loop 从“一次 LLM 调用”改造成“LLM -> tools -> LLM”的循环。

将 tool result 加入 session messages，作为模型继续推理的上下文。

记录 tool 调用 trace，方便调试。

5.2 为什么需要 Tool
没有 tool 时，claw 只能依赖 prompt 和模型参数中的知识回答问题。例如用户问：

当前项目目录下有哪些文件？
如果没有 tool，模型只能猜测，无法知道真实文件系统状态。

加入 tool 后，流程变成：

用户提问
-> 模型判断需要读取目录
-> claw runtime 调用 list_dir
-> tool 返回真实目录内容
-> 模型基于 tool result 回答用户
这就是最基本的外部反馈闭环：

reason -> act -> observe -> reason
5.3 Tool 基本要求
Tool 是 runtime 暴露给模型的一组可调用能力。模型看到的是 tool 的说明，真正执行的是 runtime 中的 handler。

一个 tool 可以拆成两部分理解：

tool definition
  给模型看的说明，告诉模型这个 tool 叫什么、能做什么、需要什么参数。

tool handler
  runtime 中真正执行的函数，负责读取时间、列目录或读文件，并返回结果。
每个 tool 至少需要表达以下信息：

name
  tool 的唯一名称。模型通过这个名称请求调用 tool。

description
  tool 的能力说明。模型依靠它判断什么时候该调用这个 tool。

input schema
  tool 参数格式。runtime 需要用它检查模型给出的参数是否合理。

handler
  runtime 中真正执行操作的函数。模型不能直接执行 handler。

safety level
  tool 的能力边界。本 Step 中统一为 read_only。
字段命名和实现方式可以自行设计，但必须满足：

模型能知道有哪些 tool、各自能做什么、需要什么参数。

runtime 能根据 tool name 找到对应实现。

runtime 会校验参数，而不是直接信任模型输出。

runtime 执行 handler 后，会把 tool result 返回给模型继续推理。

本 Step 中所有 tool 的 safety level 都必须是 read_only。

模型只能提出 tool call，不能自己真正读取文件或执行代码。真正的 tool 执行必须由 claw runtime 完成。

5.4 必须实现的只读 Tool
本 Step 至少实现 3 个只读 tool，建议覆盖以下能力：

读取当前时间
列出目录内容
读取文本文件
具体 tool 名称可以自行设计，但验收时需要能够完成以下问题：

当前时间是多少？
列出当前项目目录。
读取 README.md 并总结项目内容。
讲解这个仓库。
5.5 只读边界
本 Step 中所有 tool 都必须是只读的。

允许实现：

current_time
list_dir
read_file
建议暂不实现以下工具（后续大家在Step-8 中会实现更加复杂的工具，当前实现下面的工具可能会对本地的文件等重要内容产生比较严重的结果，如误删文件）

write_file
edit_file
delete_file
run_shell
install_package
git_commit
send_email
post_message
本 Step 不要求实现目录沙箱、路径白名单或敏感文件过滤。read_file 和 list_dir 只需要把读取结果或错误信息返回给 agent loop。

基础要求：

文件不存在时返回明确错误。

文件过大时可以截断或报错，避免一次性塞入过长上下文。

只读边界是本 Step 的唯一强制边界。

5.6 Tool Registry
Tool registry 是 runtime 管理 tools 的地方。它不一定要很复杂，但需要形成一个清晰边界：

tool definition
  给模型看的 tool 说明。

tool handler
  runtime 中真正执行 tool 的函数。

tool registry
  保存 tool definition 和 handler，并根据 tool name 找到对应 tool。
本 Step 中，tool registry 至少需要支持：

注册 tool
列出可用 tool definitions
根据 tool name 执行对应 handler
返回统一格式的 tool result 或错误
这样 agent loop 只需要处理“模型请求了哪个 tool”，不需要把每个具体 tool 的执行细节都写在 loop 里。

5.7 Tool Call 协议
如果使用的 LLM API 支持 function calling 或 tool calling，可以直接使用 API 原生 tool call 格式。

如果使用的 API 不支持 tool calling，也可以让模型输出结构化 JSON。

Tool call 的第一原理是：模型只负责提出“我要调用哪个工具、参数是什么”，runtime 才负责真正执行。执行结果必须再喂回模型，模型才能基于真实 observation 生成最终回答。

单个 tool call 的 JSON 协议可以长这样：

{
  "type": "tool_call",
  "tool": "read_file",
  "args": {
    "path": "README.md"
  }
}
如果同一轮需要读取多个信息，可以一次请求多个 tool call，最多 5 个：

{
  "type": "tool_calls",
  "calls": [
    {
      "tool": "current_time",
      "args": {}
    },
    {
      "tool": "read_file",
      "args": {
        "path": "README.md"
      }
    }
  ]
}
最终回答可以长这样：

{
  "type": "final",
  "content": "README.md 说明这个项目是一个最小 agent runtime。"
}
无论采用哪种方式，都必须满足：

模型可以表达“请求调用一个或多个 tool”和“输出最终回答”两种结果。

runtime 能解析模型输出。

runtime 能区分 tool call 和 final answer。

模型请求不存在的 tool 或参数错误时，runtime 能返回清晰错误。

如果模型在 JSON 前后混入解释文本，runtime 应尽量抽取其中合法的协议 JSON。

不能靠随意字符串匹配判断 tool call。

协议格式可以自行设计，但应在 README 或代码注释中说明，便于验收。

5.8 Agent Loop 改造
在前面的 Step 中，agent loop 大致是：

用户输入
-> buildContext
-> callLLM
-> 保存 assistant 回复
-> 返回
加入 tool 后，需要支持内部循环：

用户输入
-> buildContext
-> callLLM
-> 如果模型返回 final，则结束
-> 如果模型返回 tool_call 或 tool_calls，则执行本轮 tool
-> 将本轮 tool results 加入 session messages
-> 再次 buildContext
-> 再次 callLLM
-> 直到得到 final
不设置整个 agent turn 的总迭代次数上限。限制放在单轮 tool 批量上：模型一次最多请求 5 个 tool call，runtime 执行后把结果反馈给模型，再由模型决定继续调用工具还是输出 final。

5.9 Tool Result 消息
Tool 执行结果需要加入当前 session，作为模型下一轮推理的上下文。

要求：

tool result 属于 session messages。

tool 成功和失败都需要反馈给模型。

tool 失败不等于程序崩溃。

旧的 tool result 可以作为历史消息被压缩。

tool definition 本身不参与 compaction。

如果 tool 执行失败，也应该把错误作为 observation 反馈给模型，让模型基于真实错误继续回答用户，而不是假装 tool 成功。

5.10 上下文更新
本 Step 中，在原有上下文之外，加入 tool 使用说明。

原来的上下文结构是：

system prompt
soul
memory
session summary
recent session messages
加入 tool 后，需要让模型知道：

available tools
tool call protocol
如果使用原生 tool calling API，tool definitions 可以通过 API 的 tools 参数传入，而不是拼进 system message。课程实现不强制具体方式，但协议必须清晰。

5.11 最小程序行为
程序运行后，可以看到 claw 主动调用 tool：

$ python -m src.main

claw started. Type /exit to quit.

User> 列出当前项目目录。
[tool_call] list_dir {"path": "."}
[tool_result] ...
Assistant> 当前目录包含若干文件和子目录。
仓库讲解示例：

User> 讲解这个仓库。
[tool_call] list_dir {"path": "."}
[tool_call] read_file {"path": "README.md"}
[tool_call] list_dir {"path": "src"}
...
Assistant> 这个仓库实现了一个最小 agent runtime。入口、LLM client、session store、memory、context builder、compaction 和 tools 分别位于不同模块中。
Step 6：Gateway 与图形化操作入口
到 Step 5 为止，claw 已经具备了最小 agent runtime：它可以维护 session，构造上下文，调用 LLM，执行只读 tool，并把 tool result 反馈回 agent loop。

但是，此时 claw 主要仍然通过 CLI 与用户交互。CLI 适合调试，却不是一个便于操作的交互界面。我们希望能通过网页、QQ/微信/飞书机器人等图形化界面，实现与 claw 的交互。

为了支持与图形化界面交互，本 Step 要引入 Gateway。Gateway 是外部入口和 claw agent runtime 之间的服务层：

外部图形化界面：负责收集用户输入、展示消息、上传附件。

Gateway：负责接收外部请求、识别 session、调用已有 agent runtime、返回结果。

Agent runtime：继续负责上下文构造、memory、compaction、tool 调用和最终回答。

整体工作逻辑可以理解为：

图形化入口发送请求
-> Gateway 接收请求
-> Gateway 找到或创建 session
-> Gateway 调用已有 agent loop
-> agent loop 复用 context、memory、compaction 和 tool
-> Gateway 把 assistant 回复或错误返回给入口
Gateway 不是新的 agent。它只是把原来只能从 CLI 进入 claw 的能力，变成可以被图形化入口等外部入口调用的服务接口。

本 Step 需要在已有 session、context builder、memory、compaction、tool registry 和 agent loop 之上，实现 Gateway server、至少一个图形化操作入口、session 管理，以及和 session 绑定的附件上传能力。

6.1 Gateway Server
Gateway 是一个可以长期运行的 server，用来承接外部入口和 claw runtime 之间的通信。

本 Step 需要实现：

Gateway 可以独立启动，并在运行期间持续接收外部请求。

Gateway 提供清晰的通信协议，可以选择 HTTP、WebSocket、SSE 或其他本地通信方式。

Gateway 能够根据请求中的 session 信息找到或创建 session。

Gateway 将外部请求转交给已有 agent runtime 处理。

Gateway 返回 assistant 回复、session 信息和错误信息。

单次请求失败不能导致 Gateway 进程退出。

如果实现流式输出，也需要说明事件含义，例如 assistant 正在生成、tool 正在执行、最终回复完成或请求失败。

6.2 图形化操作入口
本 Step 至少需要实现一个图形化操作入口，用来验证 Gateway 不再依赖终端 stdin/stdout。

这个入口可以是网页，也可以是桌宠、桌面客户端、QQ/微信/飞书机器人或其他有图形化交互能力的入口。

图形化入口至少需要支持：

输入用户消息。

发送消息到 Gateway。

展示当前 session 的历史消息。

展示 assistant 回复。

展示已有 session 列表。

创建新 session。

切换 session。

展示 Gateway 返回的错误。

图形化入口必须满足：

图形化入口不能明文暴露 LLM API Key。

图形化入口中的消息发送必须经过 Gateway。

切换 session 后，需要展示对应 session 的历史消息。

创建新 session 后，后续消息应进入新 session。

图形化入口的具体形态、技术栈、交互风格、是否使用流式输出，都由学生自行决定。简单实现可以做成网页；有余力的同学也可以做成桌宠，或接入飞书、微信、QQ 等平台。课程只要求它能作为一个真实外部入口，完整调用自己的 claw。

6.3 Agent Runtime 复用
Gateway 接入后，CLI 和图形化入口都应该复用同一套 agent runtime。

Gateway 只能调用已有 agent loop，不能单独调用底层 LLM client。

调用形式可以类似：

runAgent(sessionId, userMessage, ...)
具体函数签名可以自行设计，但需要满足：

CLI 和 Gateway 都能调用同一套 agent loop。

Gateway 不绕过 context builder。

Gateway 不绕过 session store。

Gateway 不绕过 tool registry。

Gateway 调用后产生的 user、assistant 和 tool messages 需要进入同一个 session 历史。

Gateway 调用后仍然触发已有的 compaction、memory 和 tool 行为。

也就是说，从图形化入口发出的消息应该和从 CLI 发出的消息一样，进入同一条 agent runtime 路径。

6.4 Session 管理
Gateway 需要处理外部入口和 session 之间的关系。

本 Step 需要支持：

如果请求带有 sessionId，则使用对应 session。

如果 sessionId 不存在，需要返回错误或创建新 session，并说明策略。

如果请求没有 sessionId，可以使用默认 session 或创建新 session。

图形化入口需要能列出已有 session。

图形化入口需要能创建 session。

图形化入口需要能切换当前 session。

图形化入口切换 session 后，只能展示当前 session 的消息历史。

需要注意：

session 的真实历史仍然由 session store 管理。
Gateway 可以路由 session，但不应该另存一份独立聊天历史。
如果用户在 CLI 中创建过 session，图形化入口中也应该能看到这些 session。

6.5 附件上传与 Session 隔离
本 Step 需要让图形化入口可以向 Gateway 上传附件，以增强 Gateway 的输入能力和 session 的附件能力。

这里的附件上传不是 workspace 文件管理。附件属于当前 session，用来保存用户从图形化入口提交的材料；它不代表用户项目目录，也不代表 agent 可以修改 workspace 文件。

本 Step 需要支持：

图形化入口可以把附件上传到 Gateway；如果选择的入口平台不适合直接上传文件，需要通过 Gateway 提供等价的附件提交或关联方式，并在实现说明中写清楚。

Gateway 在 claw 运行的服务端为附件准备专门的存储位置，例如 data/、data/attachments/ 或类似目录。

附件必须和 session 绑定。

当前 session 只能看到或列出自己绑定的附件 metadata，例如文件名、大小、类型和上传时间。

一个 session 不能看到另一个 session 的附件 metadata。

推荐的存储结构可以是：

data/
  sessions/
    <sessionId>/
      attachments/
        <attachmentId-or-safe-file-name>
也可以使用其他结构，但必须保证附件和 session 的绑定关系。

如果希望 agent 后续能够使用用户上传的附件，需要先把附件 metadata 记录到 session 或 session 附件索引：

用户上传附件
-> Gateway 将附件保存到当前 session 的附件区
-> Gateway 将附件 metadata 记录到 session 或 session 附件索引
-> agent loop 通过明确的上下文看到当前 session 有哪些附件
注意：图形化入口不能为了读取附件而绕过 Gateway 直接访问服务端文件系统。Gateway 接入不应该新增修改用户项目文件、删除用户项目文件、执行 shell 命令等修改外部环境的能力。这些能力不属于本 Step 的必做内容。

Step 7：Scheduler 与定时任务
在 Step 6 中，claw 已经通过 Gateway 支持了图形化操作入口或其他外部入口调用。外部入口可以把用户消息交给同一套 agent runtime，从而复用 session、memory、compaction、tool 和 agent loop。

但此时 claw 仍然主要是“被动运行”的：只有当用户发来一条消息时，agent loop 才会被调用。很多真实 agent 场景还需要 claw 能在未来某个时间点，或按照某种重复规则自动开始工作。例如：

明天上午 9 点提醒我整理实验报告。
今晚 10 点总结一下今天的 session。
每隔一段时间检查某个信息源，并把结果记录下来。
每天晚上生成一次当天工作总结。
本 Step 要引入 Scheduler。Scheduler 是 claw runtime 外围的长期运行服务，负责保存和管理用户创建的定时任务，并在任务到期时调用已有 agent loop。

可以把定时任务理解为：未来由 Scheduler 自动发送给 claw 的一条用户消息。

Scheduler 不是新的 agent，也不是新的对话系统。它只负责“什么时候触发”和“触发后如何记录结果”；真正的上下文构造、memory、tool 调用、compaction 和最终回答，仍然由已有 agent runtime 完成。

7.1 Scheduler
Scheduler 解决的问题是：当没有新的外部消息到来时，claw 如何在未来自动开始工作？

Gateway 和 Scheduler 都属于 claw runtime 外围的服务模块，但它们的触发来源不同：

Gateway: 由外部用户消息触发，入口通常是图形化入口、CLI、Webhook 或聊天软件。

Scheduler: 由时间触发，入口是已经保存的任务计划。

它们最终都应该进入同一套 agent runtime：

图形化界面消息 -> Gateway   -> agent loop
定时任务 -> Scheduler -> agent loop
本 Step 需要支持：

Scheduler 可以长期运行，并持续检查是否有任务到期。

Scheduler 可以在程序重启后恢复未完成任务。

Scheduler 可以更新任务状态，并记录每次执行结果。

Scheduler 可以触发一次性任务和周期性任务。

Scheduler 到期执行任务时，必须调用已有 agent loop。

Scheduler 不应该绕过 session store、context builder、tool registry、memory 或 compaction。

Scheduler 可以和 Gateway 运行在同一个进程中，也可以是独立进程。课程不限制具体部署方式，但需要说明它如何启动、如何访问任务数据，以及如何调用 agent runtime。

7.2 定时任务
定时任务是 Scheduler 管理的基本对象。它表示一条未来要交给 agent runtime 执行的用户指令。

定时任务至少需要表达：

任务内容: 到期后要交给 agent loop 的用户指令。

触发计划: 任务应该在什么时候开始执行，以及是否需要重复执行。

下一次触发时间: Scheduler 下次应该检查并执行这个任务的时间。

任务状态: 用于区分等待中、执行中、已完成、已取消或执行失败等情况。

所属 session: 任务执行时应该进入哪个 session。

执行历史: 任务每次执行后的 assistant 回复、错误信息或执行摘要。

具体字段名、存储结构和 ID 格式由学生自行设计。关键是任务必须是一个可管理对象，而不是一段临时 sleep 逻辑。

本 Step 需要支持两类任务：

一次性任务: 用户指定一个未来时间点，任务到期后执行一次，执行完成后不再自动重复。

周期性任务: 用户指定一个重复规则，任务按照该规则多次触发，每次触发都会调用 agent loop，并留下执行历史。

周期性任务不要求实现完整 cron 表达式或复杂日历规则。可以支持固定间隔、每天固定时间，或其他能清晰说明的周期规则。关键是周期性任务必须真的能够重复触发，而不是只保存一个文本说明。

周期性任务需要定义清楚：

如何描述重复规则。

上一次执行失败后是否继续下一次。

任务执行时间长于间隔时如何处理。

用户取消时是否取消未来所有触发。

程序关闭期间错过的触发是否补执行。

这些边界不要求采用某一种固定答案，合理即可。

7.3 任务管理与持久化
本 Step 需要实现任务创建、任务列表、任务取消和任务持久化。

创建任务时必须提供：

任务内容。

触发时间或重复规则。

所属 session。

如果时间无法解析、重复规则无效或 session 不存在，需要返回错误，不能创建任务。

任务列表必须展示：

任务内容。

任务类型（一次性任务 or 周期性任务）。

重复规则。

下一次触发时间。

任务状态。

所属 session。

创建时间或更新时间。

任务历史必须保存每次执行的结果或失败原因。周期性任务不能只保留最后一次结果。

取消任务需要满足：

一次性任务取消后不再触发。

周期性任务取消后不再产生未来触发。

任务列表或历史中能看出任务已取消。

任务必须持久化。程序重启后，未完成任务、任务状态、重复规则、下一次触发时间和执行历史都不能丢失。

存储方式由实现自行决定，但不能破坏已有 session 数据结构，也不能让 Scheduler 另存一份完整聊天历史。

7.4 Agent Loop 接入
任务到期后，Scheduler 需要把任务内容交给已有 agent loop，而不是直接调用底层 LLM client。

整体流程可以理解为：

Scheduler 发现任务到期
-> 将任务标记为执行中
-> 把任务内容作为用户消息交给 agent loop
-> agent loop 使用已有 context builder、memory、tool 和 compaction
-> 保存 assistant 回复或错误信息
-> 更新任务状态
本 Step 需要满足：

任务执行必须进入对应 session 历史。

后续对话可以看到定时任务产生的消息和结果。

任务执行应复用 context builder、system prompt、soul、memory、session summary 和 recent session messages。

任务执行应复用已有 tool registry 和 tool trace。

任务执行产生较长历史时，仍然遵守已有 compaction 规则。

任务失败需要被记录，而不是被 Scheduler 静默吞掉。

周期性任务执行完成后，需要根据重复规则计算下一次触发时间。

定时任务需要有明确的 session 归属。创建任务时可以使用当前 session、用户显式选择的 session，或实现中定义的专门 scheduler session；无论采用哪种策略，用户都必须能清楚知道任务属于哪个 session，以及执行结果写回了哪里。

7.5 图形化入口
Step 6 已经要求实现图形化操作入口。本 Step 需要在该入口中增加基本任务管理能力。

图形化入口至少需要支持：

创建一次性定时任务。

创建周期性定时任务。

查看任务列表。

查看任务状态。

查看任务执行结果。

查看周期性任务的重复规则和下一次触发时间。

取消任务的未来触发。

看到任务所属 session。

图形化入口不需要做复杂日历组件或完整任务看板。重点是验证 Scheduler 能通过真实入口被管理，并且任务到期后能够进入已有 agent runtime。

所有任务创建、查询、取消和执行结果读取，都应该通过服务端接口完成。Gateway 和图形化入口不应该自己执行定时任务，它们只负责把任务管理请求交给服务端，并展示 Scheduler 保存的状态和结果。

Step 8：Workspace、Advanced Tool 与 Approval
到 Step 5 为止，claw 已经可以通过只读 tool 观察外部环境，例如读取当前时间、列目录、读文件。

本 Step 要让 claw 从“只能观察环境”变成“可以在用户指定项目中执行真实操作”的 agent runtime。需要引入三个核心概念：

Workspace: 当前 agent 可以操作的项目目录。

Advanced Tool: 在实现 workspace 进行隔离后可以实现的高级 tool 操作，包括修改文件，执行脚本，将文件发送到远端等，能支持 claw 实现更高级的功能。

Approval: 在执行会改变环境的操作前，先让用户确认。

其中 Advanced Tool 要求至少实现以下三类：

Update Tool: 能够在 workspace 内创建或修改文件。

Shell Tool: 能够启动 shell，并在 workspace 内执行命令或脚本。

Download Tool: 能够为 workspace 内文件创建可通过 Gateway 下载的临时入口，以支持远端通过 gateway 获取服务端的文件。

本 Step 需要在已有 tool registry、session、context builder 和 agent loop 上增加这些能力。

8.1 Workspace
Workspace 是当前 agent session 的项目根目录，也是文件读写、shell 命令、下载入口创建和 approval 判断的默认边界。

用户说：

帮我修改 README.md。
运行测试脚本。
runtime 需要知道这些操作发生在哪个项目中。这个项目目录就是 workspace。

本 Step 需要支持：

用户可以设置当前 workspace。

用户可以查看当前 workspace。

文件读取、文件修改、shell 命令和下载入口创建默认都发生在 workspace 内。

tool 参数中的相对路径需要按 workspace 解析。

不允许通过 ../ 或绝对路径绕过 workspace 边界。

需要实现 copy_attachment_to_workspace tool，将 Step 6 中通过 Gateway 上传并绑定到当前 session 的附件，拷贝到 workspace 内指定路径，以支持 claw 像操作普通 workspace 文件一样读取或处理这些附件。

copy_attachment_to_workspace 只能访问当前 session 绑定的附件，不能读取其他 session 的附件；目标路径必须按 workspace 解析，且不能写到 workspace 之外。

如果 workspace 未设置，涉及文件修改、命令执行、附件拷贝或下载入口创建的请求不能直接执行。

Workspace 可以绑定到当前 session，也可以作为 runtime 的当前配置；具体实现方式由学生自行决定，但用户必须能清楚知道 claw 当前正在操作哪个目录。

8.2 Update Tool
Update tool 用来把模型的文件修改意图转化为真实的 workspace 文件变化。

本 Step 需要至少实现以下 update tool：

create_file: 在 workspace 内创建新文件。

overwrite_file: 用新内容覆盖 workspace 内已有文件。

edit_file: 根据用户要求更新 workspace 内已有文件的部分内容。

如果实现时希望使用一个统一 tool，也可以设计成：

update_file: 通过 operation 参数区分 create、overwrite 和 edit。
无论使用多个 tool 还是一个统一 tool，都必须覆盖创建文件、覆盖文件和更新文件内容三类能力。

Update tool 的执行结果需要返回给 agent loop，至少包含：

是否成功

执行了哪个 tool

影响了哪个路径

简要结果或错误信息

Update tool 只能修改 workspace 内的文件，不能修改 workspace 外的文件，如果 tool 参数的路径在 workspace 之外，需要直接返回执行失败。

8.3 Shell Tool
Shell tool 用来让模型通过 runtime 控制一个 shell，并在 workspace 中运行命令或脚本，Shell tool 是实现 agent 自动化运行代码进行测试的重要环节。

本 Step 必须实现两个 shell tool：

new_shell: 启动一个新的 shell；如果之前已经启动过 shell，旧 shell 必须先退出；新 shell 的初始工作目录必须是 workspace，或 workspace 内的子目录。

run_command: 在当前已经启动的 shell 中运行一条命令；多次 run_command 应该复用同一个 shell；前序命令配置的 cwd、环境变量或 source 结果，可以影响后续命令；如果当前没有 shell，需要返回明确错误，提示先调用 new_shell。

run_command 的执行结果需要返回给 agent loop，至少包含：

是否成功

执行的命令

执行时的 cwd

退出码

stdout

stderr

是否超时

输出是否被截断

错误信息

Shell tool 只能在 workspace 内执行命令。执行前和执行后都需要确认 shell 的当前工作目录仍然位于 workspace 内；如果 shell 离开 workspace，runtime 应该终止当前 shell 并返回错误。

8.4 Download Tool
Claw 在 workspace 中对文件进行修改后会得到输出的文件，但如果用户是通过网页等远端通过 Gateway 和 claw 交互，则无法获取这些输出文件。Download tool 作用是把 workspace 内已有文件注册成一个可以由 Gateway 下载的临时链接，使得远端通过 Gateway 访问的用户可以获得 workspace 中的文件。

本 Step 需要实现 create_download，它为 workspace 内一个已有文件创建临时下载入口。

create_download 需要接收，要下载的文件的路径。

Runtime 执行 create_download 时，只需要在 workspace 中找到文件，并向 Gateway 注册一个可下载的临时入口。Gateway 可以把这个入口表现为 downloadId 或 downloadUrl，再交给图形化入口展示成下载按钮、下载链接或对应平台支持的文件获取方式。

create_download 不负责把文件内容返回给模型。模型只需要知道下载入口已经创建，或创建失败的简要原因。

8.5 Approval
Update tool 和 shell tool 都可能改变用户项目或运行环境，因此不能像只读 tool 一样直接执行。

当模型请求执行 update tool 或 shell tool 时，runtime 需要先创建 approval 请求，并暂停这次 tool 执行。用户批准后才真正执行；用户拒绝后不能执行。

Download tool 不需要进入这个显式 approval 流程；它的用户确认发生在前端点击下载入口时。

Approval 请求需要把这次模型请求的 tool call 表达清楚：

approvalId: 用于批准或拒绝这次请求。

tool: 请求执行的 tool 名称。

本次 tool 执行对应的参数（update tool 的文件和对应的内容，shell tool 需要执行的命令等）

Approval 的结果也需要进入 agent loop：

用户批准: runtime 执行对应 tool，并把 tool result 写入 session 历史。

用户拒绝: runtime 不执行 tool，并允许用户输入拒绝的原因以指导模型继续回答。

这样模型才能知道操作是否真实发生，并基于真实结果继续回答。

8.6 Agent Loop 接入
本 Step 的内容需要继续接入 agent loop，并要求能在 CLI 交互和图形化入口交互中，都能设置 workspace、审批 approval。下载入口仅需在图形化或其他远端入口中展示即可，CLI 不需要实现下载按钮、下载链接或下载入口展示；使用飞书、微信或 QQ 等入口的同学请自行探索如何实现从服务端下载文件。

需要满足：

模型仍然通过 tool call 表达文件修改、命令执行或创建下载入口的意图。

Update tool、shell tool 和 download tool 都注册到已有 tool registry。

Runtime 在执行 update tool 和 shell tool 前必须创建 approval。

Runtime 执行 create_download 前不需要创建显式 approval，成功后把下载入口交给 Gateway 并与图形化界面展示。

Runtime 负责等待用户决定，并在批准后执行需要 approval 的 tool。

Tool result、approval 拒绝结果和错误信息都需要进入 session messages，使得后续 LLM 调用能够看到这些结果

Gateway 和图形化入口不能直接改文件，也不能直接执行命令；它们只负责展示 approval、收集用户决定和展示下载入口。

原有只读 tool、memory、compaction 和 context builder 需要继续可用。

整体流程可以理解为：

用户请求
-> agent loop 调用 LLM
-> LLM 请求 update tool、shell tool 或 download tool
-> 如果是 update tool 或 shell tool:
   -> runtime 创建 approval
   -> 用户批准或拒绝
   -> runtime 执行 tool，或记录拒绝结果
-> 如果是 create_download:
   -> runtime 创建短期下载入口
   -> Gateway 将 downloadId 或 downloadUrl 发给前端
   -> 用户点击下载入口后，Gateway 返回文件内容
-> tool result / approval result 回到 agent loop
-> LLM 基于真实结果继续回答
Step 9：Skill System
到 Step 8 为止，claw 已经可以在用户指定的 workspace 中读取文件、修改文件、运行命令，并通过 approval 控制真实环境中的写操作。

本 Step 要让 claw 拥有一套 Skill system。Skill 是面向某类任务的可复用能力包，用来保存稳定的工作方法，例如指令、模板、检查清单、示例和参考资料。

很多重复任务都会反复用到类似内容：

某类文档应该有哪些结构。
某类材料应该如何整理和总结。
某类输出应该使用什么模板。
生成结果后应该检查哪些问题。
这些内容不适合全部写进 system prompt。system prompt 应该保持通用，而 skill 只在任务需要时进入当前 agent loop。

本 Step 需要引入三个核心概念：

Skill: 一组可复用的任务说明、模板和参考资料。

Skill Registry: 负责扫描本地 skills 目录，维护可用 skill 列表。

Skill Selection: 让用户可以显式调用 skill，也让模型可以根据任务自主选择 skill。

9.1 Skill
Skill 可以理解为“某一类任务的工作手册”。它不替代 system prompt，也不替代 memory。

它们的边界是：

system prompt: 定义 claw 的通用行为规则。

memory: 保存跨 session 的长期事实和偏好。

skill: 保存某类任务的可复用工作方法。

session messages: 保存当前对话的具体过程。

本 Step 可以使用本地目录表示 skill。一个 skill 至少包含一个 SKILL.md，也可以包含一些别的资源，例如：

skills/example-skill/
  SKILL.md 
  assets/template.md
  references/checklist.md
SKILL.md 至少需要表达：

name: skill 名称，例如 example-skill。

description: skill 适合处理什么任务，以及哪些用户请求应该考虑使用它。

instructions: 使用该 skill 时需要遵守的详细步骤。

可以使用类似下面的格式：

**---**
name: example-skill
description: 说明这个 skill 能处理什么任务，以及用户在什么情况下应该使用它。
**---**

**# Example Skill**

这里写具体工作流程、输出要求、注意事项和资源说明。
assets 和 references 目录不是强制格式。是否需要这些目录，取决于这个 skill 是否需要模板、示例、检查清单或参考资料。

完整 skill 内容只有在 skill 被选中后才加入上下文。未被选中的 skill 不应该整篇塞进每次 LLM 请求。

9.2 Skill Registry
Skill registry 负责管理本地可用 skill。

本 Step 需要支持：

扫描本地 skills/ 目录。

识别每个 skill 的名称、描述和适用场景。

列出所有可用 skill。

根据名称找到指定 skill。

加载指定 skill 的 SKILL.md 和相关资源。

生成一份轻量 skill 索引，供模型判断是否需要使用 skill。

轻量 skill 索引只应该包含短信息，例如：

name: example-skill
description: 说明这个 skill 能处理什么任务，以及用户在什么情况下应该使用它。
Registry 不负责执行任务。它只负责告诉 runtime：

有哪些 skill。
每个 skill 适合什么任务。
如何加载某个 skill 的完整内容。
真正的任务执行仍然由已有 agent loop 完成。

9.3 Skill 调用方式
Skill 需要支持两种调用方式。

第一种是用户显式调用：

/skill <skill-name> <task>
例如：

/skill course-report 帮我写一份 2000 字的思政课读书报告草稿，主题是《乡土中国》的现代意义，参考材料在 workspace 的 notes.md 中，保存为 reports/xiangtu.md。
这种情况下，runtime 直接根据 skill-name 加载对应 skill，并把 skill 内容和用户任务一起交给 agent loop。

第二种是模型自主选择。

例如用户直接输入：

帮我写一份 2000 字的思政课读书报告草稿，主题是《乡土中国》的现代意义，参考材料在 workspace 的 notes.md 中，保存为 reports/xiangtu.md。
用户没有写 /skill course-report，但这个任务明显适合 course-report。context builder 可以把轻量 skill 索引放入本轮上下文，让模型判断是否需要使用某个 skill。

一种简单实现方式是：

普通消息进入 agent loop 前，context builder 提供可用 skill 的轻量索引。

模型判断是否需要使用 skill。

如果需要，模型通过明确结构或内部 tool 表达“使用某个 skill”的意图。

Runtime 根据 skill 名称加载完整 skill 内容。

Agent loop 带着 skill 内容继续完成任务。

模型自主决定使用 skill 之前必须向用户发送一个 approval 信息，以让用户知道 claw 使用了 skill 的能力。

9.4 Agent Loop 接入
Skill system 不能绕过已有 runtime。

Skill 被显式调用或被模型自主选中后，需要把以下内容加入当前 agent loop：

skill 名称

skill 描述

skill 适用场景

skill 指令

本次用户任务

需要使用的模板、检查清单或参考资料

调用来源：explicit 或 auto

Skill 调用后仍然需要复用已有能力：

session store
context builder
memory
tool registry
workspace
approval
compaction
Skill 只是为本轮任务补充上下文和工作方法，不是另一套 agent。Skill 如果需要生成文件，仍然应该通过 Step 8 的 update tool 发起文件写入，并经过 approval。

Skill 使用记录也需要保存到 session 中，至少包含：

使用了哪个 skill

所属 session

用户任务

调用来源是 explicit 还是 auto

如果是 auto，模型为什么选择该 skill

使用时间

最终输出或保存路径

9.5 需要实现的 Skills
为了验证 skill registry、显式调用和模型自主选择，本 Step 至少需要实现 3 个 skill。

其中必须包含 course-report。它只是本 Step 用来验证 skill system 的一个具体例子，不是新的概念。

course-report 面向大学课程报告场景，例如：

课程小论文
课程学习总结
课程实验报告
这个 skill 至少需要包含 SKILL.md，并说明它用于生成课程报告草稿。可以额外提供报告模板或检查清单，但不强制规定具体文件名。

使用这个 skill 时，claw 需要能根据用户给出的课程要求、主题、字数、参考材料或保存路径，生成一份结构化 Markdown 草稿，并通过已有 workspace update tool 和 approval 流程保存到 workspace。

另外两个 skill 可以自行设计，也可以选择：

material-summary: 汇总 workspace 中的学习材料、课堂笔记或调研资料。

presentation-outline: 根据主题和材料生成课堂展示大纲、讲稿提纲或 PPT 页面结构。

无论选择什么，每个 skill 都需要有清晰的 description，说明它能做什么以及什么时候应该使用，否则模型无法可靠地自主选择。

9.6 CLI 与图形化入口
CLI 至少需要支持：

/skill list: 查看可用 skill。

/skill show <skill-name>: 查看某个 skill 的基本信息。

/skill <skill-name> <task>: 显式调用 skill。

/skill usage: 查看当前 session 的 skill 使用记录。

前面 Step 已经实现图形化操作入口，该入口也需要能使用 skill：

查看可用 skill 列表。

查看 skill 描述和适用场景。

选择某个 skill 并输入任务。

在普通聊天中允许模型自主选择 skill。

展示本轮是否使用 skill，以及使用原因。

继续使用已有 approval 流程批准报告草稿保存。