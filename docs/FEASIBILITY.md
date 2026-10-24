# 观鸟/博物助手 demo 可行性（10/23 19:20–20:00 EDT 实测，Linux 主机，只用 CPU）

| 模块 | 方案 | 实测结果 |
|---|---|---|
| 语音记清单 | Higgs Realtime + iNat 名称解析工具 | 中英混说、数量、备注、改数量（“其实是 4 只”）、问清单都对；语音模式 9 条指令全部执行。⚠️ text 模式出现过一次**没调用工具却说“已添加”** |
| 名称解析 | iNat `taxa/autocomplete?locale=zh-CN` | 北美红雀/红雀/cardinal/黄腰林莺/monarch 都能解析；要按 iconic_taxon 过滤（"cardinal" 会混进虫和鱼），只说“瓢虫”这种俗名只能解析到科 |
| 附近物种 | iNat `observations/species_counts`，不需要 key | 多伦多 15 km 内、9/1 以来的鸟有 150 种，返回中英文名；虫（Insecta）和植物（Plantae）同一接口 |
| 听声识鸟 | BirdNET（birdnetlib + ai-edge-litert shim） | 北美红雀 0.97、旅鸫 0.79，冠蓝鸦那段只有 0.09（录音太弱）；2 分钟录音 CPU 不到 1.5 s |
| 拍照识别 | BioCLIP 2（pybioclip，TreeOfLife，species 级） | 7/7 top-1：红雀、冠蓝鸦、横斑林鸮、黄腰林莺、君主斑蝶、七星瓢虫、大花延龄草；CPU 约 0.3 s/张（首次加载 147 s，含下载）。⚠️ 测试图来自 Wikimedia，可能在训练集里，结果偏乐观，要用主人自己拍的照片复测 |
| 按描述识鸟 | Realtime 拿附近物种列表（加 field marks）来推断 | **弱**：没加 field marks 时旅鸫两次都错（金翅雀、棕胁秋沙鸭）；加了 field marks 是 5/8；embedding 匹配（e5/bge-m3）top-1 4–5/8。→ 只给 top-3 候选和图片，让用户确认 |
| 导出 | eBird Record Format CSV（无表头，网页 import 工具）；iNat 也支持 CSV 导入 | 只看了文档，没实际导入 |
| 相机 | 按 EXIF 时间对齐语音记录（事后导卡即可）；现场联动可用 Z8 内置 FTP/Wireless Transmitter Utility 或 NX Tether 写入文件夹，Linux 上 gphoto2 支持 Z8 | 都是文档层面，没实测 |
