---
name: image-generation
description: 用户提出生图、图生图或图片编辑需求时使用。调用 OpenAI 兼容的图像接口（默认 gpt-image-2），分辨率默认 2K、用户明确要求时才用 4K，quality 默认 high。内置 429 递增退避、参数回退与生成后程序化核验。
---

# 图像生成 / 图生图 / 图片编辑

用户提出「生图 / 图生图 / 画一张 / 修改图片 / 重绘 / 扩图」类需求时使用本技能，通过 **OpenAI 兼容的 Images API** 调用图像模型（默认 `gpt-image-2`）。

## 触发场景

- **生图**：从文本描述生成全新图片。
- **图生图**：基于用户提供的图片重绘、生成变体。
- **图片编辑**：局部修改、风格迁移、扩图、修复。

## 配置

三个环境变量，从 shell 环境或项目 `.env` 读取（见 `.env.example`）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `IMAGE_API_KEY` | API 密钥。也接受 `XCLISAI_IMAGE_API_KEY` / `OPENAI_API_KEY` | 无，必填 |
| `IMAGE_API_BASE` | 接口根地址，须含 `/v1`。也接受 `XCLISAI_API_BASE` | `https://jp.xclis.ai/v1` |
| `IMAGE_MODEL` | 模型名 | `gpt-image-2` |

查找顺序：进程环境变量 → 当前目录及父目录的 `.env` → `~/.env`。
若密钥缺失：提示用户配置 `IMAGE_API_KEY`，**绝不**自行硬编码或猜测任何密钥。
若项目是 Git 仓库：确认 `.env` 在 `.gitignore` 中，没有则加上。

## 安全红线

- 密钥只从环境变量 / `.env` 读取，**不得**写入源码、文档、聊天回复或日志。
- 不要用 `curl -v` 等会回显 `Authorization` 头的方式调用。
- 错误 body 可以展示给用户，但先扫一眼确认其中不含密钥。
- 拒绝生成有害、违规或侵权内容。

## 核心认知：分辨率 ≠ quality

这是最容易出错的地方：

- **「4K / 2K / 1K」是分辨率，由 `size` 控制，与 `quality` 无关。**
- `quality` 只接受 `low` / `medium` / `high`（个别平台还接受 `auto`），默认传 `high`。把 `"4k"` 当 quality 传会 400。
- 尺寸约束：宽高均为 16 的倍数，最大边 3840，宽高比 ≤ 3:1。

| 档位 | 横版 16:9 | 竖版 9:16 | 方形 |
|---|---|---|---|
| **2K（默认）** | `2048x1152` | `1152x2048` | `2048x2048` |
| **4K（仅用户明确要求）** | `3840x2160` | `2160x3840` | `2880x2880` |
| 1K | `1536x1024` | `1024x1536` | `1024x1024` |

用户给了具体数字（如 `1920x1080`）→ 原样使用；只给比例或方向（「16:9」「竖版」）→ 按上表映射；什么都没说 → 2K，方向由画面内容推断。

## 首选调用方式：脚本

优先用仓库内的 `scripts/generate_image.py`（纯标准库，自带退避重试、参数回退、结果核验）：

```bash
# 文生图，默认 2K 横版
python3 scripts/generate_image.py "赛博朋克风格的深夜街道，霓虹倒影" \
  --size 2048x1152 --out ./outputs

# 竖版 / 方形用比例简写
python3 scripts/generate_image.py "水墨山水立轴" --size 9:16
python3 scripts/generate_image.py "极简 logo，单色" --size square

# 图生图 / 编辑：传入原图
python3 scripts/generate_image.py "把背景换成雪山黄昏，保留人物姿态" \
  --image ./input.png

# 多张、指定文件名前缀
python3 scripts/generate_image.py "三种配色的封面草图" -n 3 --name 封面草图
```

常用参数：`--size`（`2048x1152` / `16:9` / `2k` / `4k` / `square`）、`--quality`（默认 `high`）、`--image`（图生图，可多次传）、`--mask`、`-n`、`--out`、`--name`、`--model`、`--timeout`、`--json`。

脚本退出码：`0` 成功，`1` 参数/配置错误，`2` API 调用失败，`3` 结果核验失败。`--json` 会在 stdout 输出结构化结果（含文件路径、实际尺寸、核验结论），便于程序化读取。

## 直接调 API（脚本不可用时）

请求头 `Authorization: Bearer $IMAGE_API_KEY`。用 bash curl 或 Python 发起，不要走 WebFetch/WebSearch。

**文生图** `POST ${IMAGE_API_BASE}/images/generations`：

```json
{
  "model": "gpt-image-2",
  "prompt": "<用户描述，可适度补充画面细节>",
  "size": "2048x1152",
  "quality": "high",
  "n": 1,
  "response_format": "b64_json"
}
```

**图生图 / 编辑** `POST ${IMAGE_API_BASE}/images/edits`，`multipart/form-data`：字段为 `model`、`image`（原图文件，可多个）、`prompt`、`size`、`quality`、`response_format`，局部重绘另加 `mask`。

若平台不支持 `/images/edits`（404 或参数报错），改用 `/images/generations`，把原图以 `data:image/png;base64,...` data URL 传入模型支持的图像字段。

## 超时预算

图像生成慢：1K/2K 约 1–3 分钟，4K 约 3–6 分钟。给 bash 命令留足超时。

**4K 的坑**：单次命令超时（约 3 分钟）常常短于 4K 生成耗时——请求已发出并扣费，响应却拿不到。处理方式：

1. 先告知用户 4K 可能超时且仍会扣费。
2. 引导用户在本地终端直接跑 `scripts/generate_image.py ... --size 4k`，不受会话超时限制。
3. 若坚持在会话内出 4K：只试一次，超时后**不要盲目重试**（会重复扣费），改走脚本。
4. 部分账号的异步接口（`/images/generations/async`）未开通，会返回 `async image tasks are not enabled`，不要依赖；已开通时可改为「提交任务 → 轮询」在会话内交付 4K。

## 错误诊断与重试

**任何非 200 先打印错误 body**（去密钥），看 `param` / `message` 再决策，不要盲目重试。

| 错误 | 含义 | 处理 |
|---|---|---|
| 429 `rate_limit` | 账号并发超限 | **递增退避**：60s → 90s → 120s → 150s，最多约 5 次。30s 以内的退避不够。仍失败则告知用户可能有其他客户端在占用配额 |
| 400/422 `param: quality` | 质量值不支持 | 回退 `high` → `medium` → 移除该参数 |
| 400/422 `param: size` | 尺寸不支持 | 换相邻档位 4K → 2K → 1K，或移除 size 交给模型 |
| 400/422 `param: response_format` | 不支持 b64 | 移除该参数，改从响应的 `url` 下载 |
| 400/422 其他 | 参数格式问题 | 逐个去掉非必要参数重试，每次打印错误 body |
| 401 / 403 | 密钥无效或无权限 | 提示用户检查 `IMAGE_API_KEY` |
| 404 | 路径或模型不对 | 确认 `IMAGE_API_BASE` 含 `/v1`；`GET {base}/models` 验证连通性与模型是否存在 |
| 5xx | 平台侧故障 | 退避 30s 重试 2–3 次，仍失败则如实告知用户 |

回退链在一次脚本运行内自动走完，减少往返。`generate_image.py` 已实现全部上述逻辑。

## 验证与交付

不要直接交付未经检查的图。

1. **落盘**：优先取 `data[i].b64_json` 解码写文件；没有则用 `data[i].url` 下载。
2. **程序化核验**：用 PIL 检查文件可打开、尺寸、色彩模式；若用户指定了配色或风格，缩图后统计主色是否命中色板、白底占比是否合理。核验结论写进交付说明。
3. **目视检查**：用 Read 打开图片确认内容。若预览不可用（Unsupported Image），说明已用色板/结构分析替代，并请用户打开原图确认细节。
4. **命名**：按内容语义命名，如 `20260830_山水插画.png`，多张加序号。保存到当前会话的 outputs 目录（用户另有指定则跟随）。
5. **交付**：给 `computer://` 链接，附简短说明——生成内容、实际尺寸与档位、核验结论。
6. **清理**：删除中间缩略图等临时文件。
7. **失败时**：反馈去密钥的错误信息，并给出具体建议（换 key、检查 base 地址、降档重试）。
