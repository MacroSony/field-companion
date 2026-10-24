# Higgs Realtime 实测结果（2026-10-23 16:50–17:00 EDT，Linux 主机，家里网络）

脚本：`probe.py`；原始数据：`out/results_*.json`；音频：`out/*.wav`。样本量很小（每项 2–6 次），看个大概，不是 benchmark。

## 延迟
- 文字输入 → 首个音频：0.28–0.48 s（5 次）。语音生成速度约为实时的 6–8 倍（8 s 音频约 1 s 生成完）。
- 文字输入 → 首个 token（文本模式）：约 0.11–0.13 s（推理题 0.49 s）。
- **语音输入，用户说完 → 首个音频**：server_vad 下 0.97–1.20 s（3 次）。我们算的“说完”是按 TTS 音频振幅估的，所以包含网络往返；比官方宣称的 ~700 ms 慢。
- 打断：插话音频开始发送到 response 被 cancel 约 0.28 s（其中包括 TTS 开头的静音），被打断后接得上（"tell me the ending" → 一句话说结局）。注意：打断时服务端已经生成了 19.9 s 音频，**客户端必须自己清空播放缓冲**。

## 轮次检测
- server_vad：VAD 事件很早就发出 speech_stopped/committed（说话中途），但模型还是等用户说完整句才回答，user transcript 也完整。
- **semantic_vad 反而在句中停顿处抢答**：用户说 "I'm going to a hackathon tonight. [停顿] Give me one tip…"，模型在第一句后就回了 "Good luck at the hackathon tonight!"。中文那条的转写多了个 "Give," 前缀。

## 脑子（文本模式）
- 对：时间算术、17×23、三盒水果标签题、格式指令（三条 bullet）、中文解释 spec decoding、Python 一行代码。
- 弱：话多（要一行代码给了四段解释）；**问身份时说自己是 OpenAI 的 GPT-4**（身份必须写进 instructions）；没开搜索时**编造了多伦多今天的天气**，没说自己不知道。
- 定位：适合当反应快的前台，复杂推理和长任务交给后端 agent（通过 function tool）。

## 工具
- 单次调用、链式调用（查状态 → 失败 → 发消息修复）、并行调用（一次设两个计时器）都对，不该调用时也没乱调。每步 0.3–0.4 s。
- web_search：先说一句“Hang on, let me check…” → 服务端搜索 → 自动开新的 response 回答；从提问到答完约 2.5–3 s。答案带来源；比特币价格是并列三家的报价，F1 给了具体比赛和日期（没核实对不对）。

## 文档要点（10/23 读）
- Avatar 现在有实时 session（`/v1/avatar/sessions`，需要 LiveKit Cloud，不支持自建；名额有限）。workspace 网页上看不到这个功能。
- `create_response` 和 `interrupt_response` 被接受但不起作用；一个 session 只能输出一种模态（音频或文本）；`conversation.item.create` 只能放文本；空闲 5 分钟断开；usage 一直是 null。
- 克隆的 `voice_<id>` 在 Realtime 里能用。

## 补测：委派与异步通知（probe2.py，17:0x）
- 中英混说的指令（"帮我让 backend 那个 session 跑一下全部 tests, then tell me when it's done"）：能调用 start_task，但 **session 名被改成了 "BACKEND-NEG-SESSION"**，参数需要客户端做模糊匹配或校验。
- 后台任务完成后，往对话里注入 `[NOTIFY] ...` 文本再发 response.create：**0.38 s 就主动播报**，内容准确。异步回报这个模式可行。
- 一句话里问两件事（"Which test failed first? 然后顺便问一下你是谁做的模型？"）：只回答了第一件，第二件丢了。

## 用户在 Higgs Live（官方 playground）的试用（10/23 傍晚，截图 image.png）
- 打断很快（用户主观感受）；身份正确，自称 Higgs-Live/Boson（playground 的 system prompt 设了身份）。
- 搜“今天发布的 LLM”：回答 MiniMax M3.1 Flash “今天早上在 HF 发布”。**参数（428B、1M 上下文、文本/图像/视频输入）是对的，日期错了**：HF 仓库 createdAt 是 2026-10-17，10/23 03:37Z 只是最后修改时间。搜索结果分不清“最新”和“今天”。
- 搜 Kimi：K3.1 Pro 确实是本周五发布（TNW 写的是 Friday，Kimi 博客写的是 "today"，Unite.AI 写的是 10/24，大概是北京时间），这条对。
- 同一次搜索把“let me check…”的开场白说了两遍。

## 改世界测试（probe_world.py，10/23 17:3x）：单个 session 连续 13 条指令，8 个场景工具，执行器是一个带真实状态的模拟场景
- 文本模式和语音模式（TTS 合成语音、server_vad）**13 条全部做对**。判分显示 10/11，是因为“雨停、云保留”那条是我判分写错了：云本来就是 0.2，它保持不变是对的。另外两条人工判也对：数香蕉树数出 10 棵；“把河拓宽两倍”它回答说没有这个工具。
- 做对的包括：理解上下文（“再多种五棵”）；undo；一句话里两个动作；“2050 年最坏情景”对应到 RCP8.5；先调 get_scene 再算出两块田的中点 (60,0)；带条件的删除（只删玉米地里的橡树）。
- 语音模式：转写有一处小错（“五棵”听成“五克”），没影响执行。每条耗时 4–10 s，这个数包含用户说话本身的时长。
- 文本模式下，用户说过一次中文之后，它对后面的英文指令也一直用中文回答；语音模式没这个问题。
- 局限：场景简单，工具是高层宏，空间关系用的是地标枚举。比较虚的审美指令（"make it feel more like Ghana"）和以玩家视角为参照的相对方位都没测。

## 版本、视觉、鸟声（10/23 18:xx）
- `GET /v1/models` 返回：higgs-realtime（max_model_len 32768）、higgs-stt-3.1、higgs-tts-v3、higgs-avatar、higgs-realtime-tts、qwen3-forced-aligner-0.6b、higgs-tts-mps；**没有版本号**，session.created 里也只写 "higgs-realtime"。hackathon 的 starter 仓库（hackathon-m3-api-card-public）里另有一个 `https://hackathon.boson.ai/v1`：higgs-audio-understanding-v3.5/v3-Hackathon，音频进、文本出，chat completions 接口，v3.5 支持 `<tools>` 和 "Use Thinking."。用我们的 key 访问返回 503，可能需要活动专用的 key。
- **视觉：没有**。往 conversation.item.create 里放 input_image，服务端 schema 直接报 400；user 消息只接受 input_text 和 input_audio。
- **鸟声识别：0/3，而且是自信地答错**。用 Wikimedia/XC 的录音测：北美红雀答成 Song Sparrow，旅鸫答成 Black-capped Chickadee，冠蓝鸦答成 White-breasted Nuthatch。text 模式下它会说自己听不了音频文件。→ 鸟声识别必须接 BirdNET 之类的工具。
