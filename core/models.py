"""模型能力表 —— 多网关 CLI 的地基。

为什么必须有这张表，而不能"用到再说"：

  1. **元数据覆盖不全**。DeepInfer 的 /models 给上下文长度和图像/推理标志；
     Boyue 405 个模型只给 supported_endpoint_types，**没有上下文长度、没有
     能力标志**。想在 Boyue 上做模型选择器，只能靠实测 + 落盘。

  2. **参数兼容性因模型而异，且会静默 400**。实测 2026-08-21：
     claude-opus-4-8 对 `temperature` 直接返回
     "`temperature` is deprecated for this model"。硬编码 temperature=0.3
     的请求在当前世代 Claude 上全部失败。这类事实只能实测得出。

  3. **列得出 ≠ 调得通**。kimi-k3 仍在 /models 列表里但已 404；
     gpt-5.2-codex / gpt-5.6-luna 返回 400（走的不是 chat/completions）；
     qwen3-coder-plus 超时。

所以：元数据能拿的就拿，拿不到的实测一次并缓存，结果写
`~/.zylab/models.json`。探测有成本（每个模型一次真实请求），所以按需触发、
可显式刷新，不在每次启动时重跑。
"""
from . import paths
import hashlib
import json
import re
import os
from . import wincompat
import tempfile
import time
import urllib.error
import uuid
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import client, model_health

CACHE = paths.state_home() / "models.json"
CACHE_VERSION = 1

# 探测用的最小工具，够判断模型会不会发 tool_call 就行
_PROBE_TOOL = [{"type": "function", "function": {
    "name": "get_file", "description": "读取文件内容",
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}}]
_PROBE_MSG = [{"role": "user", "content": "读取 /tmp/a.txt。必须使用 get_file 工具。"}]
CATALOG_FINGERPRINT_VERSION = 2
CAPABILITY_PROBE_PROTOCOL_VERSION = 2
GATEWAY_CATALOG_REVISION = {
    "deepinfer": 1,
    "boyue": 1,
}


# 能力目录只缓存会拿来编码的模型家族，避免 Boyue 的 400+ 条原始目录淹没 UI。
#
# 小目录（DeepInfer、厂商官方口）的 picker 保留其中所有实测可用模型；大目录
# （Boyue 这类聚合网关）再用通用的名字解析只取偏好家族的最新旗舰。模型来源和
# 调用路由是两回事：经 Boyue 转发的 OpenAI / Anthropic 不冒充官方直连。
FAMILIES = {
    "OpenAI":    ("gpt-", "openai/"),
    "Anthropic": ("claude-", "anthropic/"),
    "智谱":       ("glm", "chatglm", "thudm", "zai-org", "z-ai", "zhipu"),
    "moonshot":  ("kimi", "moonshot"),
    "deepseek":  ("deepseek",),
    "阿里":       ("qwen", "tongyi"),
    "intern":    ("intern",),
    "MiniMax":   ("minimax",),
}
_ALL_TOKENS = tuple(t for toks in FAMILIES.values() for t in toks)

# 家族内也有编码 CLI 用不上的：嵌入、重排、语音、图像生成
_NON_CHAT = (
    "embedding", "reranker", "rerank", "-voice", "image-gen", "image-",
    "audio", "realtime", "deep-research", "moderation", "-tts", "whisper")

# /model 列表在**大目录**上只取偏好家族的最新旗舰（用户 2026-09-24 定：
# gpt / claude / deepseek / kimi / glm / qwen；settings 的 preferred_families 可改）。
#
# **不按公司手写规则。** 用户原话：「万一哪一天我给了你别的公司的 api 呢？难道要我
# 返回 cc 再修一遍？」—— 所以这里只有一个**通用的名字解析**：把 id 拆成
#   词干（gpt）+ 版本号前的词（opus）+ 版本号（5-5）+ 版本号后的词（sol / pro / 0813）
# 再用一小组**跨厂商通用**的词决定它是不是「一个独立的旗舰」：
#   - 硬排除：同一个模型的另一种调法 —— thinking / high / responses / vl / coder /
#     128k / fp8 / `:free` …；
#   - 软偏好：同一版本里，正式版胜过 preview/beta，不带日期的别名胜过带日期的快照
#     （0731、20250929）。没有版本号、只有日期的（qwen-plus-1220）是别名的快照，不列；
#   - 视觉版（4.6v、-vision、-vl）是家族里**单独的一条线**，与文本线各自取最新
#     （用户 2026-09-24：「glm 有个视觉模型的，那个要加上，deepseek 的也是」）；
#   - 逐级回退：最新一代里有 API 命名的旗舰就只看它们；只有开源尺寸（27b、a3b）的
#     取最大的；只剩低档（mini / flash / turbo …）的才列低档。
# 然后按「家族 → 产品线 → 版本」比新旧：家族 = 词干 + 版本号前的词（claude-opus 与
# claude-sonnet 各自发版、互不压制），家族里只看**最新一代**（主版本号），这一代的每条
# 产品线各取最新可用的一个。
#
# 2026-09-24 对真实的 408 条 Boyue 目录跑，偏好六家选出 gpt-6-{sol,luna,astra}、
# claude-opus-5-5、claude-sonnet-5、deepseek-v4-pro、kimi-k3、glm-5.3、qwen3.8-max、
# qwen3.7-plus，外加视觉线 glm-5v-turbo、deepseek-v4-flash-vision-exp、qwen3-vl-plus
# —— 与人工挑的一致（tests/test_models.py 用这份真实目录钉住）。
DEFAULT_PREFERRED_FAMILIES = ("gpt", "claude", "deepseek", "kimi", "glm", "qwen")

# 同一个模型的另一种调法：思考/推理开关、接口形态、模态、专项、安全分类。
_MODE_WORDS = frozenset((
    "thinking", "think", "reasoning", "responses", "realtime", "audio", "tts",
    "asr", "transcribe", "search", "embedding", "embeddings", "embed", "rerank",
    "reranker", "moderation", "guard", "image", "ocr", "omni",
    "video", "speech", "voice", "t2i", "t2v", "i2v", "code", "coder", "codex",
    "math", "distill", "highspeed", "base"))
# 看图的版本：不是「另一种调法」，是一条独立的线（`4.6v` 的 v 也算）。
_VISION_WORDS = frozenset(("vision", "vl", "multimodal", "v"))
# 推理力度档。只在版本号**之后**才算调法（gpt-5.5-high）；在前面是家族名
# （mistral-medium-3 是一个型号，不是 gpt-5.5 的中档）。
_EFFORT_WORDS = frozenset(("high", "low", "medium", "xhigh", "none", "minimal"))
_STAGE_WORDS = frozenset((
    "preview", "beta", "alpha", "exp", "experimental", "rc", "test", "latest"))
# 同一代里的低档。
_LOWER_TIER_WORDS = frozenset((
    "mini", "nano", "lite", "flash", "flashx", "air", "airx", "small", "tiny",
    "micro", "turbo", "fast"))
# 不改变「是哪个模型」的词：开源权重的对话版都叫 -instruct。
_NEUTRAL_WORDS = frozenset(("instruct", "chat", "it"))
_SIZE_TOKEN = re.compile(r"(a?)(\d+(?:\.\d+)?)([bt])")    # 27b、a3b（激活）、1t
_CONTEXT_TOKEN = re.compile(r"\d+[km]")                    # 128k、1m
_QUANT_TOKEN = re.compile(r"fp\d+|bf16|int\d+|awq|gptq|gguf|w\d+a\d+")
_VERSION = r"\d{1,2}(?:\.\d{1,2})*"
_BARE_VERSION = re.compile(_VERSION)                       # 6、5.6
_SERIES_VERSION = re.compile(rf"([a-z])({_VERSION})([a-z]*)")   # v4、k2.6、m3
_SUFFIXED_VERSION = re.compile(rf"({_VERSION})([a-z]+)")        # 4o、5v
_STEM_VERSION = re.compile(rf"([a-z]+?)({_VERSION})([a-z]*)")   # qwen3.8、o3


ModelName = namedtuple("ModelName", (
    "stem", "family", "line", "version", "date", "size",
    "stage", "variant", "lower", "vision"))


def parse_model_name(model_id):
    """把一个模型 id 拆成可以比新旧的几部分；空 id 返回 None。

    只认名字的**形状**，不认任何一家公司：新厂商只要按行业通行的写法起名
    （词干 + 版本号 + 档位词），不用改这里就能被正确归类。
    """
    name = str(model_id or "").strip().lower().rsplit("/", 1)[-1]
    name, tagged, _tag = name.partition(":")      # OpenRouter 式 `:free`
    tokens = [token for token in re.split(r"[-_\s]+", name) if token]
    if not tokens:
        return None
    head, tail = tokens[0], tokens[1:]
    stem = re.match(r"[a-z]*", head).group(0) or head
    family_words, words, dates = [], [], []
    version = None
    size = 0.0
    variant = bool(tagged)
    attached = _STEM_VERSION.fullmatch(head)
    if attached:                                  # qwen3.8-max：版本号贴在词干上
        stem, version = attached.group(1), attached.group(2)
        if attached.group(3):
            words.append(attached.group(3))
        post = tail
    else:
        post = []
        index = 0
        while index < len(tail):
            token = tail[index]
            if token.isdigit() and len(token) >= 3:
                # 日期（2411、20250929、2025-01-25）：连同紧跟的 1–2 位数字段一起吞掉，
                # 不然 `2025-01-25` 的 `01-25` 会被读成版本号 1.25。
                dates.append(int(token))
                index += 1
                while index < len(tail) and re.fullmatch(r"\d{1,2}", tail[index]):
                    dates.append(int(tail[index]))
                    index += 1
                continue
            sized = _SIZE_TOKEN.fullmatch(token)
            if sized:
                if not sized.group(1):
                    size = max(size, _size_value(sized))
                index += 1
                continue
            if _CONTEXT_TOKEN.fullmatch(token) or _QUANT_TOKEN.fullmatch(token):
                variant = True
                index += 1
                continue
            if _BARE_VERSION.fullmatch(token):
                parts, after = [token], index + 1
                while (after < len(tail) and len(parts) < 3
                       and re.fullmatch(r"\d{1,2}", tail[after])):
                    parts.append(tail[after])
                    after += 1
                version, post = ".".join(parts), tail[after:]
                break
            series = _SERIES_VERSION.fullmatch(token)
            if series:
                version = series.group(2)
                words.append(series.group(1) + ":")
                if series.group(3):
                    words.append(series.group(3))
                post = tail[index + 1:]
                break
            suffixed = _SUFFIXED_VERSION.fullmatch(token)
            if suffixed:
                version = suffixed.group(1)
                words.append(suffixed.group(2))
                post = tail[index + 1:]
                break
            family_words.append(token)
            index += 1
    stage = lower = vision = False
    kept_family = []
    for word in family_words:
        if word in _VISION_WORDS:
            vision = True
        elif word in _MODE_WORDS or word in _STAGE_WORDS:
            variant = True
        else:
            if word in _LOWER_TIER_WORDS:
                lower = True
            kept_family.append(word)
    for token in post:
        sized = _SIZE_TOKEN.fullmatch(token)
        if token.isdigit():
            dates.append(int(token))
        elif sized:
            if not sized.group(1):
                size = max(size, _size_value(sized))
        elif (_CONTEXT_TOKEN.fullmatch(token) or _QUANT_TOKEN.fullmatch(token)
              or token in _MODE_WORDS or token in _EFFORT_WORDS
              or (token.startswith("no") and token[2:] in _MODE_WORDS)):
            variant = True
        elif token in _STAGE_WORDS:
            stage = True
        elif token in _NEUTRAL_WORDS:
            continue
        else:
            words.append(token)
    line = []
    for word in words:
        if word in _VISION_WORDS:
            vision = True
            continue
        if word in _MODE_WORDS:
            variant = True
        if word in _LOWER_TIER_WORDS:
            lower = True
        line.append(word)
    parsed_version = (tuple(int(part) for part in version.split("."))
                      if version is not None else ())
    family = "-".join([stem] + kept_family)
    return ModelName(stem, family, tuple(line), parsed_version, tuple(dates),
                     size, stage, variant, lower, vision)


def _size_value(match):
    value = float(match.group(2))
    return value * 1000 if match.group(3) == "t" else value


def _representative_key(model_id, parsed):
    """同一个家族、产品线、版本里挑谁：正式版 > 预览版，别名 > 快照，大尺寸 > 小尺寸。"""
    return (parsed.stage, bool(parsed.date), -parsed.size,
            tuple(-part for part in parsed.date), "/" in model_id,
            len(model_id), model_id)


def _preferred_stems(parsed_by_id, preferred):
    """目录里出现了偏好家族就只看它们；一个都没有（比如只接了别家的网关）就看全部。"""
    present = {parsed.stem for parsed in parsed_by_id.values()}
    wanted = {str(name).strip().lower() for name in (
        DEFAULT_PREFERRED_FAMILIES if preferred is None else preferred)}
    matched = {stem for stem in present if stem in wanted}
    for model_id, parsed in parsed_by_id.items():
        if (family_of(model_id) or "").lower() in wanted:
            matched.add(parsed.stem)              # 也认公司名：OpenAI、Anthropic
    return matched or present


def _newest_flagships(ids, *, depth, stems=None, preferred=None):
    """{(家族, 是否视觉线): [id…]}：最新一代里每条产品线最新的 depth 个版本。"""
    parsed_by_id = {}
    for model_id in dict.fromkeys(str(i) for i in ids):
        parsed = parse_model_name(model_id)
        if parsed and not parsed.variant and parsed.version:
            parsed_by_id[model_id] = parsed
    if stems is None:
        stems = _preferred_stems(parsed_by_id, preferred)
    tracks = {}
    for model_id, parsed in parsed_by_id.items():
        if parsed.stem in stems:
            tracks.setdefault((parsed.family, parsed.vision), []).append(
                (model_id, parsed))
    picked = {}
    for track, members in tracks.items():
        generation = max(parsed.version[0] for _, parsed in members)
        newest = [m for m in members if m[1].version[0] == generation]

        def tier_of(parsed):                      # 0 旗舰 · 1 开源尺寸 · 2 低档
            return 2 if parsed.lower else (1 if parsed.size else 0)

        tier = min(tier_of(parsed) for _, parsed in newest)
        wanted_lines = {parsed.line for _, parsed in newest
                        if tier_of(parsed) == tier}
        lines = {}
        for model_id, parsed in members:
            if parsed.line not in wanted_lines or tier_of(parsed) != tier:
                continue
            versions = lines.setdefault(parsed.line, {})
            held = versions.get(parsed.version)
            if held is None or (_representative_key(model_id, parsed)
                                < _representative_key(*held)):
                versions[parsed.version] = (model_id, parsed)
        chosen = []
        for versions in lines.values():
            for version in sorted(versions, reverse=True)[:depth]:
                chosen.append(versions[version][0])
        picked[track] = chosen
    return picked


# 目录多大就不再「全列」。DeepInfer 16 条、官方 DeepSeek 2 条，全列正合适；
# Boyue 408 条、OpenRouter 数百条，全列就是用户说的「不现实」。阈值取在两者之间。
LARGE_CATALOG = 40
# 每条产品线实测最新的几个版本：最新的那个坏了，列表才有上一个可退。
FLAGSHIP_PROBE_DEPTH = 2


def flagship_candidates(ids, preferred=None, depth=FLAGSHIP_PROBE_DEPTH):
    """该自动实测的旗舰：偏好家族最新一代每条产品线最新的 depth 个版本。"""
    return [model_id for chosen in _newest_flagships(
                ids, depth=depth, preferred=preferred).values()
            for model_id in chosen]


def is_large_catalog(size):
    return int(size or 0) > LARGE_CATALOG


# /workflow 的默认席位是质量白名单，不做模糊“同家族随便挑一个”降级。
# 候选顺序表达同一代旗舰的 route/别名优先级；flash/mini/turbo/旧小模型
# 即使 capability probe 成功也不会进入异构团队。
# 席位池只有四家旗舰（用户 2026-09-04 定）：Kimi / GLM / DeepSeek / Qwen(3.8-max)。
# 不必全用，同一席位可派多个节点；异构（不同模型交叉验证、不同观点碰撞）是默认期望，
# 不是门槛。MiniMax/Claude/GPT 已移出池子；顺序即未指定席位时的轮流分配顺序。
_BUILTIN_WORKFLOW_SEATS = (
    {
        "seat": "Kimi",
        "family": "moonshot",
        "candidates": (
            ("deepinfer", "kimi-k3"),
            ("boyue", "kimi-k3"),
            ("deepinfer", "kimi-k3-256k"),
        ),
    },
    {
        "seat": "GLM",
        "family": "智谱",
        "candidates": (
            # DeepInfer added glm-5.3; prefer the direct route once its
            # capability probe is healthy, with Boyue as the verified
            # fallback for accounts where the direct endpoint is unavailable.
            ("deepinfer", "glm-5.3"),
            ("boyue", "glm-5.3"),
            ("deepinfer", "glm-5.2"),
            ("boyue", "glm-5.2"),
        ),
    },
    {
        "seat": "DeepSeek",
        "family": "deepseek",
        "candidates": (
            ("deepinfer", "deepseek-v4-pro-0813"),
            ("deepinfer", "deepseek-v4-pro"),
            ("boyue", "deepseek-v4-pro-0813"),
            ("boyue", "bailian/deepseek-v4-pro"),
            ("boyue", "deepseek-v4-pro"),
        ),
    },
    {
        "seat": "Qwen",
        "family": "阿里",
        "candidates": (
            ("boyue", "qwen3.8-max"),
            ("boyue", "qwen3.7-max"),
            ("boyue", "qwen3.7-max-2026-05-20"),
        ),
    },
)

# 生效席位表：内置四席经 settings workflow.seats 覆盖后的结果（apply_workflow_seat_overrides）。
# 各处都通过 models.WORKFLOW_DEFAULT_SEATS / WORKFLOW_SEAT_NAMES 属性访问，重绑即生效。
WORKFLOW_DEFAULT_SEATS = _BUILTIN_WORKFLOW_SEATS
WORKFLOW_SEAT_NAMES = tuple(
    config["seat"] for config in WORKFLOW_DEFAULT_SEATS)
WORKFLOW_SEAT_OVERRIDES = {}
_SEAT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,23}")


def parse_seat_route(value):
    """"gateway/model" 或 [gateway, model] → (gateway, model)；gateway 必须是已知网关。"""
    if isinstance(value, str):
        gateway, slash, model = value.strip().partition("/")
        if not slash:
            return None
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        gateway, model = value
    else:
        return None
    gateway = str(gateway or "").strip().lower()
    model = str(model or "").strip()
    if gateway not in client.GATEWAYS or not model or len(model) > 256:
        return None
    return gateway, model


def apply_workflow_seat_overrides(overrides):
    """按 settings workflow.seats 重建生效席位表并重绑模块变量。

    None 移除该席位；列表/对象替换（已有）或新增（末尾追加）。返回 (生效席位表, 问题列表)，
    问题只报告不抛：一条写错的配置不该让整个席位表失效。
    """
    global WORKFLOW_DEFAULT_SEATS, WORKFLOW_SEAT_NAMES, WORKFLOW_SEAT_OVERRIDES
    problems = []
    seats = [dict(item) for item in _BUILTIN_WORKFLOW_SEATS]
    for raw_name, value in (overrides or {}).items():
        name = str(raw_name).strip()
        if not _SEAT_NAME.fullmatch(name):
            problems.append(f"无效席位名：{raw_name!r}")
            continue
        index = next(
            (i for i, item in enumerate(seats) if item["seat"] == name), None)
        if value is None:
            if index is None:
                problems.append(f"{name}: 不在席位表里，无需移除")
            else:
                seats.pop(index)
            continue
        raw = value.get("candidates") if isinstance(value, dict) else value
        family = (value.get("family") if isinstance(value, dict) else None) or name
        routes = []
        for item in (raw or []):
            route = parse_seat_route(item)
            if route is None:
                problems.append(
                    f"{name}: 无效 route {item!r}（应为 gateway/model，"
                    f"gateway ∈ {'/'.join(client.GATEWAYS)}）")
                continue
            routes.append(route)
        if not routes:
            problems.append(f"{name}: 没有有效 route，已忽略")
            continue
        entry = {"seat": name, "family": str(family)[:32],
                 "candidates": tuple(routes)}
        if index is None:
            seats.append(entry)
        else:
            seats[index] = entry
    WORKFLOW_DEFAULT_SEATS = tuple(seats)
    WORKFLOW_SEAT_NAMES = tuple(item["seat"] for item in seats)
    WORKFLOW_SEAT_OVERRIDES = dict(overrides or {})
    return WORKFLOW_DEFAULT_SEATS, problems


def workflow_seat_source(seat):
    """席位来源：builtin / user（新增）/ user（替换内置）。"""
    builtin = any(item["seat"] == seat for item in _BUILTIN_WORKFLOW_SEATS)
    overridden = WORKFLOW_SEAT_OVERRIDES.get(seat) is not None and seat in WORKFLOW_SEAT_OVERRIDES
    if overridden and builtin:
        return "user（替换内置）"
    if overridden:
        return "user"
    return "builtin"


def workflow_default_rows(rows=None, *, route_allowed=None):
    """解析高质量 workflow 候选席位；缺席宁可留空，也不降到低质模型。

    route_allowed 是可选的健康度回调 (gateway, model) -> bool。
    能力表只接受最近一次明确成功且支持工具的 route。
    """
    candidates = (
        probed_rows() if rows is None else [dict(row) for row in rows])
    by_route = {
        (str(row.get("gateway") or ""), str(row.get("id") or "")): row
        for row in candidates
    }
    selected = []
    for config in WORKFLOW_DEFAULT_SEATS:
        chosen = None
        for gateway, model in config["candidates"]:
            row = by_route.get((gateway, model))
            if (row is None
                    or row.get("status") != "ok"
                    or row.get("supports_tools") is not True):
                continue
            if route_allowed is not None:
                try:
                    if not route_allowed(gateway, model):
                        continue
                except Exception:
                    continue
            chosen = dict(row)
            chosen.update({
                "seat": config["seat"],
                "family": config["family"],
                "quality_tier": "flagship",
            })
            break
        if chosen is not None:
            selected.append(chosen)
    return selected


def workflow_seat_routes(seat, rows=None, *, route_allowed=None):
    """某个席位的全部可用候选 route（按候选顺序），供启动前探活失败时换下一条。"""
    candidates = (
        probed_rows() if rows is None else [dict(row) for row in rows])
    by_route = {
        (str(row.get("gateway") or ""), str(row.get("id") or "")): row
        for row in candidates
    }
    config = next(
        (item for item in WORKFLOW_DEFAULT_SEATS if item["seat"] == seat), None)
    if config is None:
        return []
    selected = []
    for gateway, model in config["candidates"]:
        row = by_route.get((gateway, model))
        if (row is None or row.get("status") != "ok"
                or row.get("supports_tools") is not True):
            continue
        if route_allowed is not None:
            try:
                if not route_allowed(gateway, model):
                    continue
            except Exception:
                continue
        chosen = dict(row)
        chosen.update({
            "seat": config["seat"], "family": config["family"],
            "quality_tier": "flagship"})
        selected.append(chosen)
    return selected


def is_chat_model(model_id):
    """名字上看不出「不是对话模型」就算是 —— 真能不能用交给实测。

    以前这里是 FAMILIES 白名单：不在名单里的公司，模型在**刷新目录时**就被丢掉。
    用户问的正是这个：「万一哪一天我给了你别的公司的 api 呢？」—— 接上 Mistral 的
    key，目录里一条都不会进来。
    """
    name = str(model_id or "").lower()
    return bool(name) and not any(token in name for token in _NON_CHAT)


def display_family(model_id):
    """列表「家族」那一栏：认识的用中文/公司名，不认识的就用名字的词干。"""
    known = family_of(model_id)
    if known:
        return known
    parsed = parse_model_name(model_id)
    return parsed.stem if parsed and parsed.stem else "other"


def family_of(model_id):
    """返回模型所属家族名；不属于白名单则返回 None。"""
    i = (model_id or "").lower()
    if any(t in i for t in _NON_CHAT):
        return None
    for fam, toks in FAMILIES.items():
        if any(t in i for t in toks):
            return fam
    return None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty():
    return {"version": CACHE_VERSION, "updated": None, "models": {}}


# 随仓库分发的能力表 seed。探测（每档一次真实请求）和翻厂商文档都很贵，
# 结果却一直只存在用户态的 models.json 里 —— 新 clone 从零开始，/model 看不到
# context，压缩阈值只能退回保守默认。seed 让这批事实跟着代码走。
# 由 scripts/build_model_seed.py 生成；只含对任何 pod 都成立的字段。
SEED = Path(__file__).resolve().parent / "model_seed.json"
_SEED_CACHE = None


def _seed_models():
    """读 seed；缺失或损坏都不是错误，退回空表。"""
    global _SEED_CACHE
    if _SEED_CACHE is None:
        try:
            data = json.loads(SEED.read_text(encoding="utf-8"))
            _SEED_CACHE = (data.get("models") or {}
                           if data.get("version") == CACHE_VERSION else {})
        except (OSError, json.JSONDecodeError):
            _SEED_CACHE = {}
    return _SEED_CACHE


def _load_unlocked():
    """**只读用户缓存，绝不掺 seed。**

    transaction() 会把这里的返回值原样写回磁盘（`_write_unlocked(db)`）。
    seed 若在这一层混进来，182 条别人机器上的观测就会被永久烧进用户自己的
    models.json，之后再也分不清哪条是本机测的。合并只发生在 load()。
    """
    if CACHE.is_file():
        try:
            d = json.loads(CACHE.read_text(encoding="utf-8"))
            if d.get("version") == CACHE_VERSION:
                return d
        except (OSError, json.JSONDecodeError):
            pass
    return _empty()


def load():
    """读路径：用户缓存叠在 seed 之上。返回值只用于展示与查询，不回写。"""
    # writer 使用同目录 atomic replace；reader 不必持锁，不会看到半份 JSON。
    user = _load_unlocked()
    seed = _seed_models()
    if not seed:
        return user
    # 本机实测永远压过 seed —— seed 是维护者机器上的观测，只在本机还没探过
    # 这个模型时才有话语权。整条记录替换而非字段级合并：把两台机器的观测
    # 拼在一起，会造出一个哪台机器上都不成立的记录。
    merged = dict(user.get("models") or {})
    for key, record in seed.items():
        if key not in merged:
            merged[key] = dict(record, seeded=True)
    return dict(user, models=merged)


@contextmanager
def _cache_lock():
    """跨进程串行化 models.json 的 read-modify-write。"""
    CACHE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = CACHE.with_name(CACHE.name + ".lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        wincompat.fchmod(fd, 0o600)
        wincompat.flock(fd, wincompat.LOCK_EX)
        yield
    finally:
        wincompat.flock(fd, wincompat.LOCK_UN)
        os.close(fd)


def _write_unlocked(db):
    db["updated"] = _now()
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{CACHE.name}.", suffix=".tmp", dir=CACHE.parent)
    try:
        wincompat.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle:
            json.dump(db, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, CACHE)
        os.chmod(CACHE, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def transaction(mutator):
    """锁内重读、修改、原子替换；返回 mutator 的结果。"""
    with _cache_lock():
        db = _load_unlocked()
        result = mutator(db)
        _write_unlocked(db)
        return result


def update_record(gateway, model, mutator):
    def apply(db):
        rec = db["models"].setdefault(
            key(gateway, model), {"gateway": gateway, "id": model})
        mutator(rec)
        return dict(rec)
    return transaction(apply)


def save(db):
    """兼容批量导入；锁内按 record merge，避免覆盖别的进程新增模型。"""
    try:
        incoming = dict(db.get("models") or {})

        def merge(current):
            for record_key, record in incoming.items():
                current["models"].setdefault(record_key, {}).update(record)
        transaction(merge)
    except OSError:
        pass


def key(gateway, model):
    return f"{gateway}/{model}"


def _catalog_fingerprint(record, *, gateway=None):
    """Hash catalog facts plus the client/probe protocol invalidation key."""
    gateway = str(gateway or record.get("gateway") or "")
    fields = {
        "fingerprint_version": CATALOG_FINGERPRINT_VERSION,
        "capability_probe_protocol": CAPABILITY_PROBE_PROTOCOL_VERSION,
        "gateway": gateway,
        "gateway_catalog_revision": GATEWAY_CATALOG_REVISION.get(gateway, 1),
        "id": record.get("id"),
        "max_model_len": record.get("max_model_len"),
        "supports_image_in": record.get("supports_image_in"),
        "supports_reasoning": record.get("supports_reasoning"),
        "supported_endpoint_types": record.get("supported_endpoint_types"),
    }
    encoded = json.dumps(
        fields, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _health_facade():
    # Lazy import avoids coupling the lightweight model catalog reader to the
    # session store at module import time.
    from . import store
    return store.metrics_facade()


def _capability_payload(record):
    keys = (
        "status", "supports_tools", "supports_temperature",
        "supports_image", "supports_reasoning", "context",
        "context_source", "context_upper_bound", "context_probe_state",
    )
    return {name: record.get(name) for name in keys if name in record}


def _record_probe_health(record, *, outcome=None,
                         capability_source="capability_probe"):
    """Project models.json probe evidence into the M5 health table."""
    gateway = str(record.get("gateway") or "")
    model = str(record.get("id") or "")
    if not gateway or not model:
        raise ValueError("probe health 缺少 gateway/model")
    fingerprint = record.get("catalog_fingerprint")
    if outcome is None:
        if record.get("status") == "ok":
            outcome = model_health.PROBE_SUCCESS
        # Only an explicitly classified, fingerprint-bound capability
        # rejection is stable. Generic 400/404/model errors can be transient
        # gateway routing failures and must age out for a later re-probe.
        elif (not record.get("retryable")
              and record.get("error_kind") == "capability_rejection"
              and fingerprint):
            outcome = model_health.PROBE_CAPABILITY_REJECTION
        else:
            outcome = model_health.PROBE_TRANSIENT_FAILURE
    kwargs = {
        "capability": _capability_payload(record),
        "capability_source": capability_source,
        "probe_kind": capability_source,
    }
    if fingerprint:
        kwargs["catalog_fingerprint"] = fingerprint
    if (outcome == model_health.PROBE_CAPABILITY_REJECTION
            and not fingerprint):
        outcome = model_health.PROBE_TRANSIENT_FAILURE
    return _health_facade().record_probe_outcome(
        gateway, model, outcome, **kwargs)


def _is_tool_capability_rejection(error):
    """Recognize only explicit provider statements that tools are unsupported."""
    details = error.details() if hasattr(error, "details") else {}
    if details.get("retryable"):
        return False
    kind = details.get("kind")
    if kind not in {"invalid_request", "invalid_parameter", "http_error"}:
        return False
    if (kind == "http_error"
            and details.get("http_status") not in {400, 422}):
        return False
    text = str(error).lower()
    patterns = (
        r"\btool_choice\b.{0,32}\bnot supported\b",
        r"\btools?\b\s+(?:are|is)\s+not supported\b",
        r"\b(?:tool calls?|function calls?|function calling)\b"
        r".{0,32}\bnot supported\b",
        r"\bdoes not support\b.{0,32}"
        r"\b(?:tools?|tool_choice|function calls?)\b",
        r"\bunsupported (?:parameter|feature)\b.{0,32}"
        r"\b(?:tools?|tool_choice|function calls?)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def get(gateway, model):
    """取某模型的能力记录；没有就返回保守默认（假设支持一切，让请求自己报错）。"""
    rec = load()["models"].get(key(gateway, model))
    if rec:
        return rec
    return {"gateway": gateway, "id": model, "context": None,
            "supports_tools": None, "supports_temperature": None,
            "status": "unprobed"}


def supports_temperature(gateway, model):
    """未探测过时返回 True（保守：先按支持发，失败了 client 会降级重试）。"""
    v = get(gateway, model).get("supports_temperature")
    return True if v is None else bool(v)


def context_of(gateway, model, default=128_000):
    rec = get(gateway, model)
    return rec.get("context") or client.model_limit(
        model, default=default, gateway=gateway)


# ------------------------------------------------------------------ 抓取

# ------------------------------------------------ 自适应：目录时效与可用性
# 平台的模型名单每周都在变，而且**双向**变（kimi-k3 下线又复活；
# deepseek-v4-pro 真没了）。以前只有人手动跑 /model refresh 才会更新，缓存
# 放了 18 天没人碰 —— 席位于是一直挑中一个已经 403 的 id。三条证据链：
#   1. 目录（便宜，一次 HTTP）——  listed / 上下文标称值 / 新增与下架
#   2. 真实请求的结果（免费）—— 成功是最硬的证据，失败要连着两次才算
#   3. 手动 probe（贵，每模型一次真实请求）—— /model check 才做
CATALOG_TTL_SECONDS = 24 * 3600
UNAVAILABLE_STREAK = 2      # 连续几次「已重试仍不可用」才敢把 status 降下来


def _parse_stamp(value):
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def catalog_state(gateway):
    """某网关目录的抓取记录：fetched_at / count / 上次变化。"""
    return (load().get("catalogs") or {}).get(str(gateway)) or {}


def catalog_age_seconds(gateway):
    moment = _parse_stamp(catalog_state(gateway).get("fetched_at"))
    if moment is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())


def catalog_is_stale(gateway, ttl=CATALOG_TTL_SECONDS):
    age = catalog_age_seconds(gateway)
    return age is None or age >= max(0, int(ttl))


def note_unavailable(gateway, model, message="", *, streak=UNAVAILABLE_STREAK):
    """真实请求说这个模型调不通 —— 但一次不算数。

    DeepInfer 的 403 `model_not_available` / 404 可能只是后端实例没挂载该模型
    （client 因此把它判为 retryable）。所以要**连续两次已经内部重试过的失败**
    才把 status 降到 unavailable；降级前把原状态存下来，好在恢复时还原。
    """
    def apply(rec):
        count = int(rec.get("unavailable_streak") or 0) + 1
        rec["unavailable_streak"] = count
        rec["unavailable_error"] = str(message or "")[:200]
        rec["unavailable_at"] = _now()
        if count >= max(1, int(streak)) and rec.get("status") != "unavailable":
            rec.setdefault("status_before_unavailable", rec.get("status"))
            rec["status"] = "unavailable"
    return update_record(gateway, model, apply)


def note_available(gateway, model):
    """真实成功是最硬的证据：清掉不可用标记，status 回到 ok。"""
    def apply(rec):
        rec.pop("unavailable_streak", None)
        rec.pop("unavailable_error", None)
        rec.pop("unavailable_at", None)
        rec.pop("delisted_at", None)
        rec.pop("status_before_unavailable", None)
        rec["status"] = "ok"
        rec["last_success_at"] = _now()
    return update_record(gateway, model, apply)


def clear_unavailable_if_marked(gateway, model):
    """成功路径上的便宜守卫：只有确实带着不可用标记时才写文件。"""
    record = get(gateway, model)
    if (record.get("unavailable_streak")
            or record.get("status") in {"unavailable", "delisted"}):
        return note_available(gateway, model)
    return None


class CatalogError(RuntimeError):
    """取目录失败。`unreachable` 为真表示请求根本没到达网关（超时、连不上），
    这时该提示的是路由（代理），不是 key。"""

    def __init__(self, message, *, unreachable=False):
        super().__init__(message)
        self.unreachable = unreachable


def refresh_catalog(gateway):
    """抓目录、并进缓存、返回这次的变化。fetch_catalog 是它的计数版封装。"""
    try:
        listing = client.list_models(gateway=gateway)
    except Exception as e:
        unreachable = (
            isinstance(e, (TimeoutError, OSError))
            and not isinstance(e, urllib.error.HTTPError))
        raise CatalogError(
            f"{gateway} 取模型列表失败: {e}",
            unreachable=unreachable) from None

    changes = {"added": [], "delisted": [], "restored": []}

    def merge(db):
        seen = set()
        for m in listing:
            mid = m.get("id")
            if not mid or (gateway != "deepinfer" and not is_chat_model(mid)):
                continue
            k = key(gateway, mid)
            seen.add(k)
            rec = db["models"].get(k, {})
            if not rec:
                changes["added"].append(mid)
            elif rec.get("listed") is False:
                changes["restored"].append(mid)
            if rec.get("status") == "delisted":
                # 回到目录里了：状态退回 listed，等一次真实 probe 才敢再叫 ok
                rec["status"] = "listed"
                rec.pop("delisted_at", None)
            rec.update({
                "gateway": gateway, "id": mid,
                # DeepInfer 给这些；Boyue 全是 None —— 差异如实保留，别编。
                "context": m.get("max_model_len") or rec.get("context"),
                "supports_image": m.get(
                    "supports_image_in", rec.get("supports_image")),
                "supports_reasoning": m.get(
                    "supports_reasoning", rec.get("supports_reasoning")),
                "endpoints": (
                    m.get("supported_endpoint_types") or rec.get("endpoints")),
                "listed": True,
                "family": family_of(mid),
                "catalog_fingerprint": _catalog_fingerprint(
                    m, gateway=gateway),
            })
            rec.setdefault("supports_tools", None)
            rec.setdefault("supports_temperature", None)
            rec.setdefault("status", "listed")
            db["models"][k] = rec
        # 目录里消失的：**标记，不删除**。成员资格会来回抖动（kimi-k3-256k
        # 09-07 不在列表、09-08 又在，且全程可调用），而且 delisted ≠ 不可调用。
        # 但 status 必须从 ok 落下来 —— 席位选择只看 status=="ok"，否则会一直
        # 挑中一个已经 403 的 id（deepseek-v4-pro 就是这样卡了两周）。
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for record_key, record in db["models"].items():
            if (record_key.startswith(gateway + "/")
                    and record_key not in seen
                    and record.get("listed")):
                record["listed"] = False
                record["delisted_at"] = stamp
                changes["delisted"].append(record_key.split("/", 1)[1])
                if record.get("status") == "ok":
                    record["status"] = "delisted"
        count = sum(
            1 for record_key in db["models"]
            if record_key.startswith(gateway + "/"))
        db.setdefault("catalogs", {})[gateway] = {
            "fetched_at": stamp, "count": count,
            "listed": len(seen),
            **({"last_change": stamp} if any(changes.values()) else {}),
        }
        return count
    count = transaction(merge)
    return {"count": count, "gateway": gateway, **changes,
            "changed": bool(any(changes.values()))}


def fetch_catalog(gateway):
    """只读地把某网关的 /models 元数据并进缓存，不做任何真实推理请求。"""
    return refresh_catalog(gateway)["count"]


# ------------------------------------------------------------------ 实测

# 推理强度的可选值**来自网关**，不是写死的名单。要问两步（2026-09-24 实测 Boyue）：
#  1. 发一个不存在的值 → 拒绝原话列出**接口层**的通用枚举（none … max 七个），
#     但这不是这个模型收的：gpt-6-astra 就不收 none；
#  2. 从两头挨个试枚举里的值（最常被个别模型拒的是 none / max）：一旦被拒，拒绝原话
#     里就是**这个模型**的完整清单（「'none' is not supported with the 'gpt-6-astra-…'
#     model. Supported values are: 'low', …」）；全都收下，就是整份枚举。
# 第 2 步被收下的那几次是真推理，所以输出上限压到 16 token；每个模型只问一次。
_EFFORT_PROBE_VALUE = "zylab-effort-probe"


def parse_supported_values(message):
    """从「… Supported values are: 'a', 'b', and 'c'.」里取出 ['a', 'b', 'c']。"""
    text = str(message or "")
    marker = text.find("Supported values are")
    if marker < 0:
        return []
    return re.findall(r"'([^']+)'", text[marker:].split("\n", 1)[0])


def _try_effort(gateway, model, value, timeout):
    """发一次最小请求：('ok', None) 收下了；('rejected', [可选值]) 拒了并列出清单；
    ('error', None) 别的失败（网络、限流…）。"""
    try:
        for _ in client.stream_chat(
                model, [{"role": "user", "content": "ok"}], gateway=gateway,
                max_tokens=16, temperature=None, effort=value,
                effort_fallback=False, retries=1, timeout=timeout,
                route_explicit=True):
            pass
    except client.APIError as exc:
        options = [option for option in parse_supported_values(exc)
                   if option != _EFFORT_PROBE_VALUE]
        return ("rejected", options) if options else ("error", None)
    return "ok", None


def _edges_first(values):
    """[a, b, c, d, e] → [a, e, b, d, c]：两头的值最可能被个别模型拒。"""
    remaining, ordered = list(values), []
    while remaining:
        ordered.append(remaining.pop(0))
        if remaining:
            ordered.append(remaining.pop())
    return ordered


def discover_effort_options(gateway, model, timeout=30):
    """问网关这个模型收哪些推理强度；问出来就记进能力表并返回，问不出返回 None。"""
    outcome, enum = _try_effort(gateway, model, _EFFORT_PROBE_VALUE, timeout)
    if outcome != "rejected":
        return None               # 不校验这个值、或者别的失败：给不出选项
    options = list(enum)
    for value in _edges_first(enum):
        outcome, listed = _try_effort(gateway, model, value, timeout)
        if outcome == "rejected":
            options = listed
            break
        if outcome == "error":
            return None           # 半路失败就别记一个半截的清单
    update_record(gateway, model, lambda rec: rec.update(effort_options=options))
    return options


class EmptyProbeResponse(RuntimeError):
    """实测请求成功了，但模型什么都没回（没有文本、没有工具调用）。

    这不是能力证据：2026-09-24 实跑 claude-opus-5-5，第一次空响应被记成「不支持
    工具」、从列表里消失，第二次它好好地调了工具。
    """


# 空响应重试时给的输出额度：推理模型可能把 120 个 token 全花在思考上。
_PROBE_RETRY_MAX_TOKENS = 1024


def probe(gateway, model, timeout=90):
    """对单个模型做一次真实请求，测出：能不能调通、支不支持工具、收不收
    temperature、首 token 多久。结果写缓存。

    先带 temperature 发；若因 temperature 被拒就去掉重发 —— 这样一次探测
    同时得到「可用性」和「参数兼容性」两个事实。
    """
    existing = get(gateway, model)
    rec = {
        "gateway": gateway, "id": model, "probed": _now(),
        "catalog_fingerprint": existing.get("catalog_fingerprint"),
    }
    probe_started = time.monotonic()
    attempt_count = 0
    route_warnings = []
    try:
        temp_ok = True
        temperature_retried = False
        try:
            calls, _txt, timing = _one_shot(
                model, temperature=0.3, timeout=timeout, gateway=gateway,
                route_warning=route_warnings.append)
            attempt_count += timing["attempts"]
        except client.APIError as e:
            msg = str(e)
            if "temperature" in msg.lower():
                temp_ok = False
                temperature_retried = True
                attempt_count += int(getattr(e, "attempt_count", 1))
                calls, _txt, timing = _one_shot(
                    model, temperature=None, timeout=timeout,
                    gateway=gateway,
                    route_warning=route_warnings.append)
                attempt_count += timing["attempts"]
            else:
                raise
        if not calls and not str(_txt or "").strip():
            # 一次都没说话，谈不上「不会调工具」：给足额度再问一次。
            calls, _txt, timing = _one_shot(
                model, temperature=(0.3 if temp_ok else None),
                timeout=timeout, gateway=gateway,
                route_warning=route_warnings.append,
                max_tokens=_PROBE_RETRY_MAX_TOKENS)
            attempt_count += timing["attempts"]
            if not calls and not str(_txt or "").strip():
                reason = timing.get("finish_reason") or "?"
                # 2026-09-24 实测 claude-opus-5-5 经 Boyue/Bedrock：不带 temperature
                # 的同一条实测提示词，三次里两次 content_filter、0 token，一次正常调工具。
                hint = ("——网关的内容过滤拦下了实测提示词，下次刷新会再测；"
                        "也可以直接 /model 切过去用"
                        if reason == "content_filter" else "")
                raise EmptyProbeResponse(
                    f"空响应（finish_reason={reason}）：两次都既没有文本也没有"
                    f"工具调用，不作能力结论{hint}")
        rec.update({
            "status": "ok",
            "supports_tools": bool(calls),
            "supports_temperature": temp_ok,
            # 兼容旧 UI 的字段名，但值现在是真正的 first delta，
            # 不再拿完整响应耗时冒充 TTFT。
            "first_token_s": timing["first_delta_s"],
            "headers_s": timing["headers_s"],
            "response_s": timing["response_s"],
            "probe_total_s": round(time.monotonic() - probe_started, 3),
            "probe_attempts": max(1, attempt_count),
            "probe_timing_version": 2,
            "temperature_retried": temperature_retried,
            # 没指定强度时网关回报的就是模型默认（2026-09-24 实测两个 gpt-6 都是 medium）
            **({"effort_default": timing["effort"]} if timing.get("effort") else {}),
            # 这次真的发出去了：之前「明文 HTTP 未授权」留下的拦截标记作废，
            # 否则界面会对一次成功的实测说「未实测」（2026-09-24 qwen3.8-max）。
            "probe_blocked": None,
            "probe_blocked_at": None,
            "error": None,
            "error_kind": None,
            "http_status": None,
            "error_code": None,
            "provider_request_id": None,
            "retryable": None,
            "route_warning": route_warnings[-1] if route_warnings else None,
        })
    # Route outage is not capability evidence. Freshness backoff in model_health
    # prevents hot-loop probes, so transient failures can safely preserve a
    # prior tool verdict (or remain unknown) instead of lying with False.
    except client.APIError as e:
        # 本地传输策略拒发（明文 HTTP 未授权）**不是**模型的能力证据：请求
        # 根本没出这台机器。记成 error 会把一个健康模型误降级 —— 2026-09-08
        # 实测：非交互脚本里 probe boyue，两个健康席位被一起打成 error。
        if getattr(e, "kind", "") == "insecure_transport":
            rec.update({
                "status": existing.get("status") or "listed",
                "probe_blocked": "insecure_transport",
                "probe_blocked_at": _now(),
                "probe_total_s": round(time.monotonic() - probe_started, 3),
            })
            saved = update_record(gateway, model, lambda old: old.update(rec))
            return saved
        attempt_count += int(getattr(e, "attempt_count", 1))
        details = e.details()
        capability_rejection = _is_tool_capability_rejection(e)
        rec.update({
            "status": "error", "error": str(e)[:200],
            "probe_blocked": None, "probe_blocked_at": None,
            "supports_tools": (
                False if capability_rejection
                else existing.get("supports_tools")),
            "supports_temperature": existing.get("supports_temperature"),
            "first_token_s": None, "headers_s": None,
            "response_s": None, "temperature_retried": False,
            "probe_total_s": round(time.monotonic() - probe_started, 3),
            "probe_attempts": max(1, attempt_count),
            "probe_timing_version": 2,
            "error_kind": (
                "capability_rejection" if capability_rejection
                else details.get("kind")),
            "http_status": details.get("http_status"),
            "error_code": details.get("code"),
            "provider_request_id": details.get("provider_request_id"),
            "retryable": details.get("retryable"),
        })
    except Exception as e:
        rec.update({
            "status": "error",
            "error": f"{type(e).__name__}: {e}"[:200],
            "supports_tools": existing.get("supports_tools"),
            "supports_temperature": existing.get("supports_temperature"),
            "first_token_s": None, "headers_s": None,
            "response_s": None, "temperature_retried": False,
            "probe_total_s": round(time.monotonic() - probe_started, 3),
            "probe_attempts": max(
                1, attempt_count + int(getattr(e, "attempt_count", 1))),
            "probe_timing_version": 2,
            "error_kind": type(e).__name__,
            "http_status": None, "error_code": None,
            "provider_request_id": None, "retryable": None,
        })

    saved = update_record(gateway, model, lambda old: old.update(rec))
    _record_probe_health(saved)
    if saved.get("status") == "ok" and saved.get("api") == "responses":
        # 走 Responses 的模型能选推理强度：顺手问出它收哪些值（校验失败不耗 token）。
        try:
            discover_effort_options(gateway, model)
            saved = get(gateway, model)
        except Exception:                             # noqa: BLE001
            pass                  # 问不出来不影响实测结论
    return saved


def _one_shot(model, temperature, timeout, gateway=None,
              route_warning=None, max_tokens=120):
    calls, txt = [], ""
    finish_reason = None
    used_effort = None
    phases = client.RequestPhases()
    kw = {
        "tools": _PROBE_TOOL,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "phases": phases,
        # Probe 必须亲自观察 temperature rejection；若让 client 静默降级，
        # 能力表会把“不支持”误记成“支持”。普通聊天仍保留自动降级。
        "temperature_fallback": False,
        "trace_context": {
            "request_id": f"probe-{uuid.uuid4().hex}",
            "context_tokens_before": None,
            "context_limit": None,
            "cwd": os.getcwd(),
            "raw": {"purpose": "capability_probe"},
        },
        # A probe is an explicit request for this exact route and must be able
        # to test whether an open circuit has recovered.
        "route_explicit": True,
        "route_warning": route_warning,
    }
    # **必须显式传**，包括 None：stream_chat 的 temperature 默认值是 0.3，只在「没
    # 传」时生效 —— 以前 None 时干脆不传，于是「去掉 temperature 重发」又把 0.3 带了
    # 回去（2026-09-24 实跑 kimi-k3，第二次仍报 `'temperature'=0.3 is not supported`）。
    kw["temperature"] = temperature
    try:
        for ev in client.stream_chat(
                model, _PROBE_MSG, gateway=gateway, **kw):
            if ev["t"] == "tool":
                calls = ev["v"]
            elif ev["t"] == "text":
                txt += ev["v"]
            elif ev["t"] == "done":
                finish_reason = ev.get("reason")
                used_effort = ev.get("effort")
    except Exception as exc:
        # 把内部 transient/parameter attempts 带回 probe 记录；不改变异常类型。
        try:
            exc.attempt_count = max(1, len(phases.snapshot()))
        except Exception:
            pass
        raise
    attempts = phases.snapshot()
    latest = attempts[-1] if attempts else {}

    def elapsed(end, start):
        if end is None or start is None:
            return None
        return round(max(0.0, float(end) - float(start)), 3)

    timing = {
        "attempts": max(1, len(attempts)),
        "headers_s": elapsed(
            latest.get("headers_at"), latest.get("started_at")),
        "first_delta_s": elapsed(
            latest.get("first_delta_at"), latest.get("started_at")),
        "response_s": elapsed(
            latest.get("ended_at"), latest.get("started_at")),
        "finish_reason": finish_reason,
        "effort": used_effort,
    }
    return calls, txt, timing


# 同名模型两边都有时的优先级。DeepInfer 免费且直连，优先；
# Boyue 作为兜底（可报销，但要走裸 IP，且模型多但元数据少）。
GATEWAY_PRIORITY = ("deepinfer", "boyue")


def resolve(model, prefer=None):
    """给一个模型名，决定该用哪个网关。

    两边都有就按 GATEWAY_PRIORITY 选（默认 deepinfer 优先）。
    只有一边有就用那边。都没有则返回 (None, model)，交给调用方决定。
    """
    db = load()["models"]
    order = ([prefer] if prefer else []) + [g for g in GATEWAY_PRIORITY if g != prefer]
    for gw in order:
        if key(gw, model) in db:
            return gw, model
    return None, model


def where(model):
    """这个模型在哪些网关上有。"""
    db = load()["models"]
    return [g for g in GATEWAY_PRIORITY if key(g, model) in db]


def _catalog_size(gateway, db=None, rows=None):
    """网关目录有多大：上次刷新时在册的条数与缓存里的记录数取大者。"""
    db = load() if db is None else db
    meta = (db.get("catalogs") or {}).get(gateway) or {}
    listed = int(meta.get("listed") or meta.get("count") or 0)
    if rows is None:
        rows = probed_rows(gateway)
    return max(listed, len(rows))


def probe_targets(gateway, preferred=None):
    """/model refresh 与 --probe-all 该实测谁 —— 与 /model 列表同一套规则。

    小目录全部实测（列表也全列）；大目录只测偏好公司每条产品线最新的几个版本，
    列表也只从这些里挑。两边用同一条规则，才不会出现「列表想显示、却从没被测过」。
    """
    rows = probed_rows(gateway)
    if is_large_catalog(_catalog_size(gateway, rows=rows)):
        # 大目录只从**还在目录里**的挑：已下架的旗舰不会是「最新」，测它只是每次
        # 刷新都白花一次请求（2026-09-24 实跑：claude-fable-5 / 5-1 下架后仍被测）。
        listed = [row["id"] for row in rows if row.get("listed") is not False]
        return flagship_candidates(listed, preferred=preferred)
    # 小目录照旧全测，含已下架的：成员资格会来回抖动，列不出 ≠ 调不通。
    return [row["id"] for row in rows]


def due_for_probe(gateway, ids, *, revalidate=True):
    """ids 里哪些该实测：没有记录的、健康度判定到期的（含目录指纹变了）。

    `revalidate=False` 给 /model refresh 用：**本机实测过能用的不重测**。用户定的是
    「新旗舰刷新时实测一次」，重新验证老模型是 /model check 的事 —— 2026-09-24 实跑，
    成功有效期（24 h）一过，一次刷新把 DeepInfer 上 20 个早就能用的模型全重测了一遍，
    整条命令 204 s。seed 里的记录是别人机器上的观测，不算「本机测过」。
    """
    db = load()["models"]
    facade = _health_facade()
    health_by_model = {
        row["model"]: row
        for row in facade.list_health(gateway=gateway, limit=10_000)
    }
    decision_now = time.time()
    due = []
    for mid in ids:
        rec = db.get(key(gateway, mid))
        if (not revalidate and rec and rec.get("status") == "ok"
                and rec.get("supports_tools") is not None
                and not rec.get("seeded")):
            continue
        if rec:
            decision = model_health.probe_decision(
                health_by_model.get(mid),
                catalog_fingerprint=rec.get("catalog_fingerprint"),
                now=decision_now)
            if not decision.due:
                continue
        due.append(mid)
    return due


def probe_many(gateway, ids=None, on_result=None, skip_probed=True):
    """批量探测并落盘。返回 (成功数, 失败数, 跳过数)。

    每个模型一次真实请求，逐个写缓存 —— 中途中断也不丢已测结果。
    """
    if ids is None:
        ids = probe_targets(gateway)
    ids = list(ids)
    todo = due_for_probe(gateway, ids) if skip_probed else ids
    skipped = len(ids) - len(todo)
    ok = bad = 0
    for mid in todo:
        r = probe(gateway, mid)
        if r.get("status") == "ok":
            ok += 1
        else:
            bad += 1
        if on_result:
            on_result(r)
    return ok, bad, skipped


def probed_rows(gateway=None, only_ok=False):
    """给 UI 用的排序好的行。"""
    db = load()["models"]
    rows = []
    for record in db.values():
        route = str(record.get("gateway") or "")
        if gateway is not None and route != gateway:
            continue
        if only_ok and record.get("status") != "ok":
            continue
        if route != "deepinfer" and not is_chat_model(record.get("id", "")):
            continue
        row = dict(record)
        row["family"] = (
            family_of(row.get("id", "")) or row.get("family") or "other")
        rows.append(row)
    rows.sort(key=lambda r: (
        r.get("gateway", ""), -(r.get("context") or 0), r.get("id", "")))
    return rows


def _flagship_rows(rows, preferred=None):
    """大目录：偏好家族最新一代里每条产品线**最新的、实测可用的**那一个。

    「最新的」坏了就退到上一个能用的 —— 不能让一家整个消失。2026-09-24 的现场：
    名单里的 claude-opus-5 / claude-sonnet-5 在 Boyue 上实测 error，于是列表里
    一个 Claude 都没有。只看实测可用（status ok 且确认能调工具）：刚刷进来、还没
    实测的新旗舰不冒充可用，由 /model refresh 的自动实测把它们测出来。

    偏好家族按**整个目录**判定而不是按「可用的」判定：一家暂时全挂不该让列表
    突然换成「所有家族」。
    """
    ids = [str(row.get("id") or "") for row in rows]
    parsed = {model_id: parse_model_name(model_id) for model_id in ids}
    stems = _preferred_stems(
        {k: v for k, v in parsed.items() if v and not v.variant and v.version},
        preferred)
    usable = {}
    for row in rows:
        if row.get("status") == "ok" and row.get("supports_tools") is True:
            usable[str(row.get("id") or "")] = row
    picked = _newest_flagships(usable, depth=1, stems=stems)
    return [usable[model_id] for chosen in picked.values()
            for model_id in chosen]


def default_picker_rows(rows=None, *, catalog_sizes=None, preferred=None):
    """返回 /model 工作集。

    小目录（DeepInfer、厂商官方口）：所有实测可用的，含明确「仅聊天」的（picker
    灰显）；大目录（Boyue 这类聚合网关）：偏好公司的最新旗舰。未知能力、实测失败
    的都不冒充可用项；不认识的网关名不列。

    `catalog_sizes` 给显式传入的 rows 用（{网关: 目录条数}）；缺省时按 rows 里
    该网关的条数算。rows 缺省时读缓存，目录大小也从缓存的刷新记录里取。
    """
    if rows is None:
        rows = probed_rows()
        db = load()
        sizes = {gateway: _catalog_size(gateway, db=db, rows=[])
                 for gateway in (db.get("catalogs") or {})}
    else:
        rows = [dict(row) for row in rows]
        sizes = {}
    sizes.update(catalog_sizes or {})
    groups = {}
    for row in rows:
        groups.setdefault(str(row.get("gateway") or ""), []).append(row)
    selected = []
    for route, group in groups.items():
        if route not in client.GATEWAYS:
            continue
        size = max(int(sizes.get(route) or 0), len(group))
        if is_large_catalog(size):
            chosen = _flagship_rows(group, preferred)
        else:
            # 小目录：明确可聊天但无 tool_call 的模型也保留，由 picker 灰显；
            # 未知能力仍不冒充可用。
            chosen = [row for row in group
                      if row.get("status") == "ok"
                      and row.get("supports_tools") in (True, False)]
        for row in chosen:
            row["family"] = display_family(row.get("id", ""))
            selected.append(row)
    return selected


def keyed_gateways():
    """配了 key、也有 endpoint 的网关 —— /model refresh 要刷的就是它们。

    以前只刷「当前网关」：用户往 keys.env 里加了官方 DeepSeek 的 key，而当前网关
    是 boyue，于是 DeepSeek 的目录从来没被取过，列表里自然一条都没有。
    """
    names = []
    for name in client.GATEWAYS:
        try:
            route = client.route_for(name)
        except ValueError:
            continue
        if not route.base:
            continue
        try:
            found = client.key_status(route).get("found")
        except OSError:
            found = False
        if found:
            names.append(name)
    return names


# 上下文探测的基础阶梯。必须从小到大逐档走，并在最后一档仍成功时继续扩张；
# 旧实现只在这张表内二分，1M 仍成功就写「未触顶」，那不是完整 probe。
CTX_LADDER = (32_000, 64_000, 128_000, 200_000, 256_000, 512_000, 1_000_000)

# 两个当前网关的请求体上限约 6 MB。探测填充单元 ``" x"`` 在目标模型家族的
# 常见 tokenizer 中通常接近一个 token，同时只占 2 bytes；按 95% 填充时，3M
# 档约 5.7 MB。若这一档仍成功，只能证明模型至少收得下这么多，不能冒充模型顶。
CTX_PROBE_MAX_TOKENS = 3_000_000
_CTX_FILL_UNIT = " x"
_CTX_FILL_RATIO = 0.95

# 网关有 6 MB 请求体上限，超过就先撞这个而不是模型上限 —— 别把它误当成模型能力。
_BODY_LIMIT_HINTS = (
    "max bytes to request body", "request entity too large",
    "payload too large", "content length limit",
)


# 明确表示「输入太长」的错误特征。只有命中这些才算模型装不下；
# 其余（超时、5xx、连接失败）都是**偶发故障**，必须重试而不能当成上限 ——
# 否则一次网络抖动就会把 1M 的模型永久记成「连 32k 都不收」。
_TOO_LONG = ("exceeds max length", "maximum context", "context_length",
             "too long", "reduce the length", "max_model_len",
             "上下文超长", "输入过长")


# 错误信息里如果直接写了上限，那是最高质量的信号 —— 比任何推断都准。
# 见过的形式："maximum context length is 128000 tokens"、"max_model_len (131072)"。
_LIMIT_RE = re.compile(
    r"(?:maximum\s+context\s+length\s+is|max_model_len|maximum\s+context)"
    r"[^0-9]{0,16}(\d{4,8})", re.I)


def looks_too_long(msg):
    """这条报错是不是「输入太长」。只有它才是上限信号，其余都是偶发故障。"""
    return any(t in (msg or "").lower() for t in _TOO_LONG)


def parse_limit(msg):
    """能从报错里直接抠出上限就抠，抠不到返回 None。"""
    m = _LIMIT_RE.search(msg or "")
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 1_000 <= n <= 20_000_000 else None


class ContextAttempt:
    """一次 context 档位请求的可判定结果。

    ``prompt_tokens`` 来自 provider usage，是成功下界的首选证据；没有 usage 时
    调用方才退回目标档位，并把结果标成 estimated。
    """

    __slots__ = ("fits", "prompt_tokens", "stated_limit", "attempts", "error")

    def __init__(self, fits, *, prompt_tokens=None, stated_limit=None,
                 attempts=1, error=None):
        self.fits = bool(fits)
        self.prompt_tokens = prompt_tokens
        self.stated_limit = stated_limit
        self.attempts = max(1, int(attempts))
        self.error = error

    def __bool__(self):
        return self.fits


class ContextProbeBlocked(RuntimeError):
    """基础设施/瞬时故障使本次请求不能用来判断模型上下文。"""

    def __init__(self, message, *, attempts=1, kind="transient"):
        super().__init__(message)
        self.attempts = max(1, int(attempts))
        self.kind = str(kind)


def thinking_off_rejected(gateway, model):
    """这条 route 是否已知「不许关思考」（关了就 400）。见 note_thinking_off_rejected。"""
    try:
        return get(gateway, model).get("thinking_off") == "rejected"
    except Exception:                                  # noqa: BLE001 - 目录坏了不该挡住请求
        return False


def note_thinking_off_rejected(gateway, model):
    """记下「这条 route 关思考会被 400」——同样是**零成本测量**：证据是一对真实请求
    （带 thinking=disabled 被 400，同一 route 不带就成功），不是对报错文案的猜测。

    2026-09-20 实测：glm-5.3@boyue 对 thinking=disabled 一律 400（code 1210「该模型始终
    思考，不支持关闭思考」），压缩请求在这条用户最常用的 route 上 33 次全部失败、从未成功。
    学到之后，摘要/ recap 这类旁路请求就不再先白撞一次。
    """
    def apply(rec):
        rec["thinking_off"] = "rejected"
    try:
        update_record(gateway, model, apply)
    except OSError:
        pass


def compact_needs_wide_budget(gateway, model):
    """这条 route 写摘要是否已知「小额度写不完」。见 note_compact_needs_wide_budget。"""
    try:
        return bool(get(gateway, model).get("compact_wide_budget"))
    except Exception:                                  # noqa: BLE001 - 目录坏了不该挡住请求
        return False


def note_compact_needs_wide_budget(gateway, model):
    """记下「这条 route 的摘要在小额度里写不完」—— 证据是一对真实请求：小额度那次
    截断、同一 route 加大额度成功。不是对模型脾气的猜测。

    2026-09-24 实测 claude-opus-5-5@boyue：连续 4 段压缩，每段 6000 额度那次都在 6000
    处截断、16000 那次都通过（写了 8.6K–9.9K），每段白跑 60–65 s。Claude 的分词器数中文
    比我们的估算多约六成，同样的内容它就要更多额度。学到之后直接从大额度开始。
    """
    def apply(rec):
        rec["compact_wide_budget"] = True
    try:
        update_record(gateway, model, apply)
    except OSError:
        pass


def note_context_ok(gateway, model, prompt_tokens):
    """记录「这个模型确实收下过这么多 token」—— 只涨不跌的已知下界。

    这是**零成本测量**：请求本来就要发，usage.prompt_tokens 是白送的。
    只在刷新纪录时才落盘，所以正常使用下几乎不写文件。
    """
    if not prompt_tokens or prompt_tokens <= 0:
        return None

    def apply(rec):
        if prompt_tokens > (rec.get("context_seen_ok") or 0):
            rec["context_seen_ok"] = int(prompt_tokens)
    rec = update_record(gateway, model, apply)
    return rec.get("context_seen_ok")


def note_context_reject(gateway, model, msg, attempted_tokens=None):
    """记录「这个模型拒收了」—— 已知上界，同样零成本（被拒的请求基本不计费）。

    返回可用作上限的估计值。优先级：
      1. 报错里明写的上限（最准）
      2. 撞墙时的规模（保守，宁可低估 —— 高估会让压缩不触发、反复撞墙）
    并写回 context，标记来源为 learned，下次开会话直接用，不会再撞第二次。
    """
    stated = parse_limit(msg)
    est = stated or (int(attempted_tokens) if attempted_tokens else None)
    if not est or est <= 0:
        return None
    def apply(rec):
        prev = rec.get("context_reject_at")
        rec["context_reject_at"] = min(prev, est) if prev else est
        # 学到的上限：明写的直接用；只能靠撞墙估的，取「撞墙规模」和「已知收过的
        # 最大值」中较大者 —— 后者是硬证据，不能被一次保守估计压到它下面。
        seen = rec.get("context_seen_ok") or 0
        rec["context"] = stated or max(rec["context_reject_at"], seen)
        rec["context_source"] = (
            "learned-stated" if stated else "learned-rejected")
    rec = update_record(gateway, model, apply)
    return rec["context"]


def _ctx_fits(gateway, model, n_tokens, timeout=180, retries=2):
    """发一个约 n_tokens 的输入，看模型收不收。

    返回 ContextAttempt：bool(result) 表示收得下/明确因过长被拒。
    偶发故障会重试；重试用尽仍失败则抛 RuntimeError —— 由调用方决定放弃，
    **不能**把它记成上下文上限。
    用短 ASCII token 单元填充，尽量在 6 MB request-body 内测到更高档位；成功时
    仍以 provider 返回的 prompt_tokens 为准，不把构造目标冒充精确 token 数。
    """
    filler = _CTX_FILL_UNIT * max(1, int(n_tokens * _CTX_FILL_RATIO))
    last = None
    for attempt in range(retries + 1):
        try:
            prompt_tokens = None
            for ev in client.stream_chat(
                    model, [{"role": "user", "content": filler + "\n回复 OK 即可"}],
                    max_tokens=4, temperature=None, retries=1,
                    timeout=timeout, gateway=gateway,
                    trace_context={
                        "request_id": f"context-probe-{uuid.uuid4().hex}",
                        "context_tokens_before": int(n_tokens),
                        "context_limit": None,
                        "cwd": os.getcwd(),
                        "raw": {
                            "purpose": "context_probe",
                            "target_tokens": int(n_tokens),
                        },
                    },
                    route_explicit=True):
                if ev["t"] == "done":
                    usage = ev.get("usage") or {}
                    raw_tokens = (
                        usage.get("prompt_tokens")
                        or usage.get("input_tokens"))
                    try:
                        value = int(raw_tokens)
                    except (TypeError, ValueError):
                        value = 0
                    if value > 0:
                        prompt_tokens = value
            return ContextAttempt(
                True, prompt_tokens=prompt_tokens, attempts=attempt + 1)
        except client.Interrupted:
            raise
        except client.APIError as e:
            msg = str(e)
            low = msg.lower()
            if any(hint in low for hint in _BODY_LIMIT_HINTS):
                raise ContextProbeBlocked(
                    "撞到网关请求体上限，不是模型上下文上限",
                    attempts=attempt + 1, kind="request_body") from None
            if looks_too_long(msg):
                return ContextAttempt(
                    False, stated_limit=parse_limit(msg),
                    attempts=attempt + 1, error=msg[:200])
            last = msg                        # 其余一律视为偶发，重试
        except Exception as e:                # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
    raise ContextProbeBlocked(
        f"重试 {retries} 次仍失败，无法判定：{str(last)[:120]}",
        attempts=retries + 1, kind="transient")


def _context_probe_targets(ladder=CTX_LADDER,
                           max_probe_tokens=CTX_PROBE_MAX_TOKENS):
    """生成完整升序探顶计划；基础 ladder 成功后继续倍增到可测安全上限。"""
    ceiling = int(max_probe_tokens)
    if ceiling <= 0:
        raise ValueError("max_probe_tokens 必须 > 0")
    targets = sorted({
        int(value) for value in ladder
        if int(value) > 0 and int(value) <= ceiling
    })
    if not targets:
        targets = [min(32_000, ceiling)]
    while targets[-1] < ceiling:
        candidate = min(ceiling, targets[-1] * 2)
        if candidate <= targets[-1]:
            break
        targets.append(candidate)
    return tuple(targets)


def probe_context(gateway, model, ladder=CTX_LADDER, on_progress=None,
                  max_probe_tokens=CTX_PROBE_MAX_TOKENS):
    """逐档扩张，直到明确收到 context-too-long 拒绝或达到可测安全上限。

    返回 (下界, 上界)：下界是实测收得下的最大值，上界是实测被拒的最小值。
    只有明确过长拒绝才写 ``context_probe_state=bounded``；达到 request-body
    安全上限仍成功则写 ``ceiling``，明确表示真实模型顶尚未确认。
    """
    targets = _context_probe_targets(ladder, max_probe_tokens)
    started = time.monotonic()
    lo_ok = lo_target = 0
    hi_bad = rejected_at = stated_limit = None
    lower_estimated = False
    steps = http_requests = 0

    def emit(state, **values):
        if on_progress is None:
            return
        event = {
            "state": state, "gateway": gateway, "model": model,
            "completed": steps, "total": len(targets),
            "steps": steps, "requests": http_requests,
            "lower": lo_ok or None, "upper": hi_bad,
            "elapsed_s": round(time.monotonic() - started, 3),
        }
        event.update(values)
        on_progress(event)

    emit("start", target=targets[0])
    for index, n in enumerate(targets, 1):
        emit("request", target=n, step=index, completed=index - 1)
        try:
            result = _ctx_fits(gateway, model, n)
        except ContextProbeBlocked as e:
            steps = index
            http_requests += e.attempts
            message = str(e)[:160]
            blocked = update_record(
                gateway, model,
                lambda rec: rec.update(
                    context_probe_state="blocked",
                    context_probe_error=message,
                    context_probe_blocker=e.kind,
                    context_probe_lower_bound=lo_ok or None,
                    context_probe_lower_target=lo_target or None,
                    context_probe_rejected_at=None,
                    context_probe_stated_limit=None,
                    context_probe_lower_estimated=lower_estimated,
                    context_probe_steps=steps,
                    context_probe_requests=http_requests,
                    context_probe_completed=_now(),
                    context_probe_version=2))
            _record_probe_health(
                blocked,
                outcome=model_health.PROBE_TRANSIENT_FAILURE,
                capability_source="context_probe")
            emit("blocked", target=n, outcome="blocked", error=message,
                 blocker=e.kind, completed=steps, total=steps)
            return None, None

        # 兼容测试/第三方对私有 helper 的旧 bool mock；真实实现返回 ContextAttempt。
        attempts = int(getattr(result, "attempts", 1))
        http_requests += max(1, attempts)
        steps = index
        if bool(result):
            observed = getattr(result, "prompt_tokens", None)
            candidate_lower = int(observed or n)
            if candidate_lower >= lo_ok:
                lo_ok = candidate_lower
                lo_target = n
                lower_estimated = observed is None
            emit("result", target=n, result="ok", observed=observed,
                 estimated=lower_estimated)
        else:
            rejected_at = n
            stated_limit = getattr(result, "stated_limit", None)
            hi_bad = int(stated_limit or n)
            if lo_ok and hi_bad <= lo_ok:
                # 成功 usage 是硬证据；服务端文案若与它矛盾，保留实际拒绝档位。
                hi_bad = n if n > lo_ok else lo_ok + 1
            emit("result", target=n, result="rejected",
                 stated_limit=stated_limit, upper=hi_bad)
            break

    if hi_bad is None:
        message = (
            f"已探测到 request-body 安全上限 {targets[-1]:,} tokens，"
            "模型仍接受；真实上下文上限尚未确认")

        def apply_floor(rec):
            rec.update({
                "context": lo_ok,
                "context_source": "probed-floor",
                "context_upper_bound": None,
                "context_probe_state": "ceiling",
                "context_probe_error": message,
                "context_probe_blocker": "request_body_ceiling",
                "context_probe_lower_bound": lo_ok,
                "context_probe_lower_target": lo_target,
                "context_probe_rejected_at": None,
                "context_probe_stated_limit": None,
                "context_probe_lower_estimated": lower_estimated,
                "context_probe_steps": steps,
                "context_probe_requests": http_requests,
                "context_probe_completed": _now(),
                "context_probe_version": 2,
            })
        floor = update_record(gateway, model, apply_floor)
        _record_probe_health(
            floor, outcome=model_health.PROBE_SUCCESS,
            capability_source="context_probe")
        emit("done", outcome="ceiling", target=targets[-1],
             completed=steps, total=steps, error=message)
        return lo_ok, None

    def apply_bound(rec):
        if lo_ok:
            rec["context"] = lo_ok
            rec["context_source"] = "probed"
        rec.update({
            "context_upper_bound": hi_bad,
            "context_probe_state": "bounded",
            "context_probe_error": None,
            "context_probe_blocker": None,
            "context_probe_lower_bound": lo_ok or None,
            "context_probe_lower_target": lo_target or None,
            "context_probe_rejected_at": rejected_at,
            "context_probe_stated_limit": stated_limit,
            "context_probe_lower_estimated": lower_estimated,
            "context_probe_steps": steps,
            "context_probe_requests": http_requests,
            "context_probe_completed": _now(),
            "context_probe_version": 2,
        })
    bounded = update_record(gateway, model, apply_bound)
    _record_probe_health(
        bounded, outcome=model_health.PROBE_SUCCESS,
        capability_source="context_probe")
    emit("done", outcome="bounded", target=rejected_at,
         completed=steps, total=steps)
    return lo_ok, hi_bad
