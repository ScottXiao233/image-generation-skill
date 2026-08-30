# image-generation skill

一个给 Claude 用的图像生成技能：文生图、图生图、图片编辑，走 **OpenAI 兼容的 Images API**（默认模型 `gpt-image-2`）。

技能文档把踩过的坑都写进了规则里，所以模型不会再犯这几类常见错误：

- 把「4K」当成 `quality` 传（`quality` 只有 `low/medium/high`，分辨率归 `size` 管）
- 遇到 429 用 30 秒退避（不够，实测要 60s 起递增）
- 4K 请求超时后盲目重试，重复扣费
- 交付没核验过的图（脚本会检查尺寸、色彩模式、是否近乎纯色）

## 安装

**Claude Code / CLI**

```bash
git clone https://github.com/ScottXiao233/image-generation-skill.git \
  ~/.claude/skills/image-generation
```

**Claude 桌面 app**：打开 Settings → Capabilities → Skills，上传本仓库打包成的 `.skill`（把目录压缩后改扩展名即可），或直接把目录放进 app 的 skills 目录。注意 CLI 的 `~/.claude/skills` 和桌面 app 的 skill 目录是两套，互不相通，两边都要用就装两次。

装好后目录长这样：

```
image-generation/
├── SKILL.md                  # 技能规则，Claude 读这个
├── scripts/generate_image.py # 可独立运行的 CLI
└── .env.example
```

## 配置

```bash
cp .env.example .env   # 然后填入密钥
```

| 变量 | 说明 | 默认 |
|---|---|---|
| `IMAGE_API_KEY` | API 密钥，必填。也接受 `XCLISAI_IMAGE_API_KEY` / `OPENAI_API_KEY` | — |
| `IMAGE_API_BASE` | 接口根地址，须含 `/v1` | `https://jp.xclis.ai/v1` |
| `IMAGE_MODEL` | 模型名 | `gpt-image-2` |

查找顺序：进程环境变量 → 当前目录及父目录的 `.env` → `~/.env`。密钥绝不会被写进日志或输出，脚本对错误信息做了脱敏。**记得把 `.env` 加进 `.gitignore`。**

默认地址指向 Xclis.ai，但任何 OpenAI 兼容的图像端点都能用——换 `IMAGE_API_BASE` 和 `IMAGE_MODEL` 即可（官方 OpenAI 用 `https://api.openai.com/v1` + `gpt-image-1`）。

## 命令行用法

脚本只依赖标准库，Python 3.9+ 直接跑。装了 Pillow 会额外做图像核验。

```bash
# 文生图，默认 2K，方向按描述推断
python3 scripts/generate_image.py "赛博朋克风格的深夜街道，霓虹倒影"

# 比例简写
python3 scripts/generate_image.py "水墨山水立轴" --size 9:16
python3 scripts/generate_image.py "极简单色 logo" --size square

# 4K（较慢，3-6 分钟）
python3 scripts/generate_image.py "桌面壁纸，雪山日出" --size 4k --out ~/Pictures

# 图生图 / 编辑
python3 scripts/generate_image.py "背景换成雪山黄昏，保留人物姿态" --image input.png

# 局部重绘
python3 scripts/generate_image.py "把杯子换成花瓶" --image in.png --mask mask.png

# 多张 + 结构化输出
python3 scripts/generate_image.py "三种配色的封面草图" -n 3 --name 封面草图 --json
```

`--size` 接受 `2048x1152`、`16:9`、`9:16`、`square`、`2k`、`4k`、`1k 16:9`、`none`（交给模型决定）。

退出码：`0` 成功、`1` 参数或配置错误、`2` API 调用失败、`3` 结果核验没通过。`--json` 会在 stdout 打印文件路径、实际尺寸、主色和核验结论。

## 分辨率对照

宽高须为 16 的倍数，最大边 3840，宽高比 ≤ 3:1。

| 档位 | 横版 16:9 | 竖版 9:16 | 方形 |
|---|---|---|---|
| **2K（默认）** | `2048x1152` | `1152x2048` | `2048x2048` |
| **4K** | `3840x2160` | `2160x3840` | `2880x2880` |
| 1K | `1536x1024` | `1024x1536` | `1024x1024` |

## 自动回退链

一次运行内自动处理，不用手动重试：

| 情况 | 行为 |
|---|---|
| 429 并发超限 | 退避 60s → 90s → 120s → 150s → 180s（带抖动） |
| 5xx | 退避 30s → 60s → 90s |
| `param: quality` | `high` → `medium` → `low` → 移除 |
| `param: size` | 4K → 2K → 1K → 移除 |
| `param: response_format` | 移除，改从响应 `url` 下载 |
| `/images/edits` 返回 404 | 改用 `/images/generations` + base64 data URL 传图 |
| 401 / 403 | 立即停止并提示检查密钥（不重试） |

## 4K 的注意事项

4K 生成要 3–6 分钟，常常超过受限环境的单次命令超时（约 3 分钟）。请求已经发出并扣费，但响应拿不到。所以要 4K 时，直接在本地终端跑脚本，别让它在受限会话里超时。超时后**不要**重试同一个请求——会重复扣费。

部分账号的异步接口（`/images/generations/async`）未开通，会返回 `async image tasks are not enabled`，不要依赖它。

## License

MIT
