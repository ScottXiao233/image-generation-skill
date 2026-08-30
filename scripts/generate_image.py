#!/usr/bin/env python3
"""OpenAI 兼容图像接口的命令行生成器（文生图 / 图生图 / 编辑）。

只依赖 Python 标准库。PIL 可选：装了就做尺寸与色彩核验，没装则跳过。

配置（进程环境变量 → 就近的 .env → ~/.env）：
    IMAGE_API_KEY   密钥，必填（亦接受 XCLISAI_IMAGE_API_KEY / OPENAI_API_KEY）
    IMAGE_API_BASE  接口根地址，需含 /v1（亦接受 XCLISAI_API_BASE）
    IMAGE_MODEL     模型名，默认 gpt-image-2

退出码：0 成功 / 1 参数或配置错误 / 2 API 调用失败 / 3 结果核验失败
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

DEFAULT_BASE = "https://jp.xclis.ai/v1"
DEFAULT_MODEL = "gpt-image-2"

KEY_VARS = ("IMAGE_API_KEY", "XCLISAI_IMAGE_API_KEY", "OPENAI_API_KEY")
BASE_VARS = ("IMAGE_API_BASE", "XCLISAI_API_BASE", "OPENAI_BASE_URL")

# 分辨率档位：landscape / portrait / square
SIZE_TABLE = {
    "1k": {"landscape": "1536x1024", "portrait": "1024x1536", "square": "1024x1024"},
    "2k": {"landscape": "2048x1152", "portrait": "1152x2048", "square": "2048x2048"},
    "4k": {"landscape": "3840x2160", "portrait": "2160x3840", "square": "2880x2880"},
}
# size 回退链：不被支持时逐级降档，最后放弃该参数交给模型
SIZE_FALLBACK = {"4k": "2k", "2k": "1k", "1k": None}
QUALITY_FALLBACK = {"high": "medium", "medium": "low", "low": None}

RETRY_WAITS = (60, 90, 120, 150, 180)  # 429 递增退避，30s 以内不够
SERVER_ERROR_WAITS = (30, 60, 90)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- 配置

def load_dotenv_chain(explicit: str | None = None) -> dict[str, str]:
    """按就近优先收集 .env：显式指定 → cwd 及各级父目录 → ~/.env。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    cwd = Path.cwd().resolve()
    candidates.extend([cwd, *cwd.parents][:6])
    values: dict[str, str] = {}
    seen: set[Path] = set()
    for cand in candidates:
        path = cand if cand.is_file() else cand / ".env"
        path = path.expanduser()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        values = {**parse_dotenv(path), **values}  # 先到者优先
    home_env = Path.home() / ".env"
    if home_env.is_file() and home_env not in seen:
        values = {**parse_dotenv(home_env), **values}
    return values


def parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:]
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        if key:
            out[key] = val
    return out


def resolve_config(args) -> tuple[str, str, str]:
    env_file = load_dotenv_chain(args.env_file)

    def pick(names, cli=None, default=None):
        if cli:
            return cli
        for name in names:
            val = os.environ.get(name) or env_file.get(name)
            if val:
                return val
        return default

    key = pick(KEY_VARS)
    base = (pick(BASE_VARS, args.base, DEFAULT_BASE) or DEFAULT_BASE).rstrip("/")
    model = pick(("IMAGE_MODEL",), args.model, DEFAULT_MODEL)
    if not key:
        log(
            "缺少 API 密钥。请设置 IMAGE_API_KEY 环境变量，或在项目 .env 中写入：\n"
            "  IMAGE_API_KEY=你的密钥\n"
            f"  IMAGE_API_BASE={DEFAULT_BASE}\n"
            "注意把 .env 加入 .gitignore，不要提交密钥。"
        )
        sys.exit(1)
    if not base.endswith("/v1") and "/v1/" not in base:
        log(f"提示：IMAGE_API_BASE 通常应以 /v1 结尾，当前为 {base}")
    return key, base, model


def redact(text: str, secret: str) -> str:
    """兜底：任何输出前去掉密钥与 Bearer 串。"""
    if secret:
        text = text.replace(secret, "***")
        if len(secret) > 8:
            text = text.replace(secret[:8], "***")
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***", text)


# ------------------------------------------------------------------ size 解析

def parse_size(spec: str | None, prompt: str) -> tuple[str | None, str]:
    """返回 (size 字符串, 所属档位)。档位用于失败时降档。"""
    if not spec:
        return SIZE_TABLE["2k"][infer_orientation(prompt)], "2k"

    s = spec.strip().lower().replace("×", "x").replace(" ", "")
    if s in {"none", "auto", "model"}:
        return None, "auto"

    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        return f"{w}x{h}", tier_of(w, h)

    tier, orient = "2k", None
    for name in ("4k", "2k", "1k"):
        if name in s:
            tier = name
            s = s.replace(name, "")
            break
    if any(t in s for t in ("16:9", "3:2", "4:3", "landscape", "横", "wide")):
        orient = "landscape"
    elif any(t in s for t in ("9:16", "2:3", "3:4", "portrait", "竖", "tall")):
        orient = "portrait"
    elif any(t in s for t in ("1:1", "square", "方")):
        orient = "square"
    if orient is None:
        orient = infer_orientation(prompt)
    return SIZE_TABLE[tier][orient], tier


def tier_of(w: int, h: int) -> str:
    longest = max(w, h)
    if longest >= 2800:
        return "4k"
    if longest >= 1600:
        return "2k"
    return "1k"


def infer_orientation(prompt: str) -> str:
    p = (prompt or "").lower()
    if any(t in p for t in ("竖版", "竖构图", "portrait", "海报", "poster", "手机壁纸", "立轴", "9:16")):
        return "portrait"
    if any(t in p for t in ("方形", "square", "头像", "avatar", "logo", "图标", "1:1")):
        return "square"
    return "landscape"


def downgrade_size(size: str | None, tier: str, prompt: str) -> tuple[str | None, str] | None:
    """size 不被支持时降一档；返回 None 表示无档可降。"""
    nxt = SIZE_FALLBACK.get(tier)
    if nxt is None:
        return (None, "auto") if size is not None else None
    return SIZE_TABLE[nxt][infer_orientation(prompt)], nxt


# --------------------------------------------------------------------- HTTP

class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body

    @property
    def param(self) -> str:
        try:
            payload = json.loads(self.body)
        except Exception:
            return ""
        err = payload.get("error") or payload
        if isinstance(err, dict):
            return str(err.get("param") or "")
        return ""

    @property
    def message(self) -> str:
        try:
            payload = json.loads(self.body)
        except Exception:
            return self.body[:600]
        err = payload.get("error") or payload
        if isinstance(err, dict):
            return str(err.get("message") or self.body[:600])
        return self.body[:600]


def post(url: str, key: str, *, payload: dict | None = None,
         fields: dict | None = None, files: list[tuple[str, Path]] | None = None,
         timeout: int = 600) -> dict:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if files:
        body, content_type = encode_multipart(fields or {}, files)
        headers["Content-Type"] = content_type
    else:
        body = json.dumps(payload or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as exc:
        raise ApiError(0, f"网络错误：{exc.reason}") from None


def encode_multipart(fields: dict, files: list[tuple[str, Path]]) -> tuple[bytes, str]:
    boundary = f"----imggen{uuid.uuid4().hex}"
    buf = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        buf += f"--{boundary}\r\n".encode()
        buf += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        buf += f"{value}\r\n".encode()
    for name, path in files:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        buf += f"--{boundary}\r\n".encode()
        buf += (
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{path.name}"\r\n'.encode()
        )
        buf += f"Content-Type: {ctype}\r\n\r\n".encode()
        buf += path.read_bytes() + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


# ------------------------------------------------------- 带回退与退避的调用

def call_with_fallback(base: str, key: str, model: str, args,
                       size: str | None, tier: str) -> tuple[dict, dict]:
    """依次处理 429 退避、5xx 重试、quality/size/response_format 回退。

    返回 (响应 JSON, 最终生效的参数字典)。
    """
    quality: str | None = args.quality
    response_format: str | None = None if args.no_b64 else "b64_json"
    images = [Path(p).expanduser() for p in (args.image or [])]
    endpoint = "images/edits" if images else "images/generations"
    rate_limit_hits = 0
    server_error_hits = 0
    edits_disabled = False

    for attempt in range(1, 40):
        url = f"{base}/{endpoint}"
        common = {
            "model": model,
            "prompt": args.prompt,
            "n": str(args.n),
        }
        if size:
            common["size"] = size
        if quality:
            common["quality"] = quality
        if response_format:
            common["response_format"] = response_format

        shown = {k: v for k, v in common.items() if k != "prompt"}
        log(f"[{attempt}] POST {endpoint} {shown}")
        try:
            if images and not edits_disabled:
                files = [("image" if len(images) == 1 else "image[]", p) for p in images]
                if args.mask:
                    files.append(("mask", Path(args.mask).expanduser()))
                data = post(url, key, fields=common, files=files, timeout=args.timeout)
            else:
                payload = dict(common, n=args.n)
                if images and edits_disabled:
                    payload["image"] = [to_data_url(p) for p in images]
                data = post(url, key, payload=payload, timeout=args.timeout)
            return data, {"size": size, "quality": quality,
                          "response_format": response_format, "endpoint": endpoint}
        except ApiError as exc:
            body = redact(exc.body, key)
            param = exc.param

            if exc.status == 429:
                if rate_limit_hits >= len(RETRY_WAITS):
                    log("429 重试已用尽：账号并发受限，可能有其他客户端在占用配额。")
                    raise
                wait = RETRY_WAITS[rate_limit_hits] + random.randint(0, 10)
                rate_limit_hits += 1
                log(f"429 并发超限，等待 {wait}s 后重试（第 {rate_limit_hits} 次退避）")
                time.sleep(wait)
                continue

            if exc.status >= 500 or exc.status == 0:
                if server_error_hits >= len(SERVER_ERROR_WAITS):
                    log(f"平台侧持续报错：{body[:400]}")
                    raise
                wait = SERVER_ERROR_WAITS[server_error_hits]
                server_error_hits += 1
                log(f"HTTP {exc.status}，等待 {wait}s 后重试。{body[:200]}")
                time.sleep(wait)
                continue

            log(f"HTTP {exc.status} param={param or '-'} :: {body[:500]}")

            if exc.status == 404 and images and not edits_disabled:
                log("edits 接口不可用，改用 generations + data URL 传图")
                edits_disabled = True
                endpoint = "images/generations"
                continue

            lowered = body.lower()
            if param == "quality" or (quality and "quality" in lowered):
                nxt = QUALITY_FALLBACK.get(quality or "", None)
                log(f"quality 回退：{quality} → {nxt or '移除该参数'}")
                quality = nxt
                continue
            if param == "size" or (size and "size" in lowered):
                step = downgrade_size(size, tier, args.prompt)
                if step is not None:
                    size, tier = step
                    log(f"size 回退 → {size or '交给模型决定'}")
                    continue
            if param == "response_format" or (response_format and "response_format" in lowered):
                log("移除 response_format，改从响应 url 下载")
                response_format = None
                continue
            if exc.status in (401, 403):
                log("密钥无效或无权限，请检查 IMAGE_API_KEY。")
                raise
            if exc.status == 404:
                log(f"路径或模型不存在。确认 IMAGE_API_BASE 含 /v1，且模型 {model} 可用。")
                raise
            # 兜底：逐个剥离非必要参数
            if response_format:
                response_format = None
                log("兜底回退：移除 response_format")
                continue
            if quality:
                quality = None
                log("兜底回退：移除 quality")
                continue
            if size:
                size = None
                log("兜底回退：移除 size")
                continue
            raise
    raise ApiError(0, "重试次数超出上限")


def to_data_url(path: Path) -> str:
    ctype = mimetypes.guess_type(path.name)[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:{ctype};base64,{b64}"


# ------------------------------------------------------------------ 落盘核验

def save_images(data: dict, out_dir: Path, name: str, key: str) -> list[Path]:
    items = data.get("data") or []
    if not items:
        raise ValueError(f"响应中没有图片数据：{redact(json.dumps(data)[:400], key)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved: list[Path] = []
    for idx, item in enumerate(items, start=1):
        suffix = "" if len(items) == 1 else f"_{idx}"
        target = out_dir / f"{stamp}_{name}{suffix}.png"
        if item.get("b64_json"):
            target.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            req = urllib.request.Request(item["url"], headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                target.write_bytes(resp.read())
        else:
            log(f"第 {idx} 张既无 b64_json 也无 url，跳过")
            continue
        saved.append(target)
    if not saved:
        raise ValueError("没有任何图片成功落盘")
    return saved


def verify(paths: list[Path]) -> tuple[bool, list[dict]]:
    """程序化核验：文件可打开、尺寸、模式、主色。PIL 缺失则只查文件大小。"""
    report: list[dict] = []
    ok = True
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        log("未安装 Pillow，跳过图像核验（pip install Pillow 可启用）")
        for p in paths:
            size = p.stat().st_size
            good = size > 1024
            ok = ok and good
            report.append({"file": str(p), "bytes": size, "ok": good,
                           "note": "仅检查文件大小（无 Pillow）"})
        return ok, report

    for p in paths:
        entry: dict = {"file": str(p), "bytes": p.stat().st_size}
        try:
            with Image.open(p) as im:
                im.load()
                entry.update(size=f"{im.width}x{im.height}", mode=im.mode)
                thumb = im.convert("RGB").resize((64, 64))
                pixels = list(thumb.getdata())
                top = sorted(
                    {tuple(round(c / 32) * 32 for c in px) for px in pixels},
                    key=lambda c: -sum(1 for px in pixels
                                       if all(abs(a - b) <= 24 for a, b in zip(px, c))),
                )[:3]
                entry["dominant_colors"] = [
                    "#%02X%02X%02X" % tuple(min(255, v) for v in c) for c in top
                ]
                flat = len({tuple(px) for px in pixels}) <= 2
                entry["ok"] = not flat
                if flat:
                    entry["note"] = "图像近乎纯色，疑似生成失败"
        except Exception as exc:  # noqa: BLE001
            entry.update(ok=False, note=f"无法打开：{exc}")
        ok = ok and bool(entry.get("ok"))
        report.append(entry)
    return ok, report


# ------------------------------------------------------------------------ CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="OpenAI 兼容图像接口生成器（文生图 / 图生图 / 编辑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  %(prog)s "赛博朋克深夜街道，霓虹倒影"\n'
            '  %(prog)s "水墨山水立轴" --size 9:16\n'
            '  %(prog)s "要 4K 壁纸" --size 4k --out ~/Pictures\n'
            '  %(prog)s "背景换成雪山黄昏" --image input.png\n'
        ),
    )
    p.add_argument("prompt", help="图像描述")
    p.add_argument("--size", "-s", default=None,
                   help="2048x1152 / 16:9 / 9:16 / square / 2k / 4k / none（默认 2K，方向按描述推断）")
    p.add_argument("--quality", "-q", default="high",
                   choices=["low", "medium", "high", "auto"], help="默认 high")
    p.add_argument("--image", "-i", action="append",
                   help="图生图/编辑的输入图片，可重复传入")
    p.add_argument("--mask", default=None, help="局部重绘遮罩（配合 --image）")
    p.add_argument("-n", type=int, default=1, help="生成张数，默认 1")
    p.add_argument("--out", "-o", default=".", help="输出目录，默认当前目录")
    p.add_argument("--name", default="image", help="文件名主体，默认 image")
    p.add_argument("--model", default=None, help=f"模型名，默认 {DEFAULT_MODEL}")
    p.add_argument("--base", default=None, help=f"接口根地址，默认 {DEFAULT_BASE}")
    p.add_argument("--env-file", default=None, help="指定 .env 路径")
    p.add_argument("--timeout", type=int, default=600, help="单次请求超时秒数，默认 600")
    p.add_argument("--no-b64", action="store_true",
                   help="不请求 b64_json，直接走 url 下载")
    p.add_argument("--json", action="store_true", help="stdout 输出结构化结果")
    return p


def main() -> int:
    args = build_parser().parse_args()
    key, base, model = resolve_config(args)

    for path in [*(args.image or []), *([args.mask] if args.mask else [])]:
        if not Path(path).expanduser().is_file():
            log(f"输入文件不存在：{path}")
            return 1

    size, tier = parse_size(args.size, args.prompt)
    log(f"模型 {model} | 目标尺寸 {size or '模型自选'}（{tier}）| quality {args.quality}")
    if tier == "4k":
        log("提示：4K 通常需要 3-6 分钟，若在受限环境中调用请留足超时。")

    started = time.time()
    try:
        data, used = call_with_fallback(base, key, model, args, size, tier)
    except ApiError as exc:
        log(f"调用失败：HTTP {exc.status} {redact(exc.message, key)}")
        return 2

    try:
        paths = save_images(data, Path(args.out).expanduser(), args.name, key)
    except Exception as exc:  # noqa: BLE001
        log(f"结果落盘失败：{redact(str(exc), key)}")
        return 2

    ok, report = verify(paths)
    elapsed = round(time.time() - started, 1)

    if args.json:
        print(json.dumps({
            "ok": ok, "elapsed_seconds": elapsed, "model": model,
            "params": used, "files": [str(p) for p in paths],
            "verification": report,
        }, ensure_ascii=False, indent=2))
    else:
        log(f"完成，用时 {elapsed}s")
        for entry in report:
            flag = "OK " if entry.get("ok") else "!! "
            detail = entry.get("size") or f"{entry['bytes']} bytes"
            note = f" — {entry['note']}" if entry.get("note") else ""
            print(f"{flag}{entry['file']}  {detail}{note}")

    return 0 if ok else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("已中断")
        sys.exit(130)
