"""AI 重绘配图：把题目里的照片/截图交给视觉或图像模型重新绘制。

动机：MinerU 从 PDF 里抠出来的配图是位图截图——有底噪、有压缩痕迹、字号与正文
不搭，放进导出的试卷里一眼就能看出是"拼的"。视觉聊天模型会重画成 TikZ，图像
模型则通过 Images Edit 生成 PNG；两者都接入同一套预览、应用和还原流程。

流程：读原图 → 按模型类型调用视觉聊天或 Images Edit → 校验生成结果 → 返回给前端
预览 → 用户点"应用"才写进正文。

三条不变量：
  1. **原图一律保留。** 应用重绘只改正文里的引用（`![[a.jpg]]` → `![[tikz_x.svg]]`），
     那张 jpg 文件不删，且原文件名记进 frontmatter 的 img_originals，随时能退回。
  2. **不信任客户端给的路径。** "应用"接口收到的 web 路径必须过 `validate_generated`，
     只接受本模块生成的 SVG/PNG 文件名，且 TikZ 产物的同名 pdf 必须在位——否则等于允许把任意字符串写进
     用户的 md 正文。
  3. **按序号替换，不用 str.replace。** 同一张图在正文里出现两次时 replace 会把两处
     都改掉（同 qrender.swap_image_refs 的取舍）。

与服务器版的差异只剩路径形态：图片引用是 Obsidian 双链 `![[文件名]]`（扁平存在
`_assets/`），没有 `/qimages/<scope>/` 前缀也没有 alt。LLM 配置与服务器版一致，
走 `providers.resolve("redraw")` —— 重绘必须是能吃图片的多模态模型，没单独配时
回落到「导入识别」那套（反方向不回落）。
"""

import hashlib
import io
import logging
import re
from pathlib import Path

from PIL import Image, UnidentifiedImageError

import config
import filestore
import llm_client
import providers
import tikz_render

logger = logging.getLogger(__name__)

PROMPT_PATH = config.BASE_DIR / "prompts" / "tikz_redraw.md"

# Obsidian 嵌入语法：![[文件名]] 或 ![[文件名|宽度]]。只有一个捕获组（文件名），
# 与 qrender._QIMG_RE 保持一致——两边对"第几张图"的计数必须完全同序。
_QIMG_RE = re.compile(r"!\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")

# 生成物文件名的形状，由 tikz_render.render 决定。校验用，别放宽。
_GEN_RE = re.compile(r"\Atikz_[0-9a-f]{16}\.svg\Z")
_GEN_IMAGE_RE = re.compile(r"\Aredraw_[0-9a-f]{16}\.png\Z")


class RedrawError(Exception):
    """重绘失败。message 直接展示给用户。"""


def load_prompt() -> str:
    if not PROMPT_PATH.is_file():
        raise RedrawError(f"找不到重绘提示词文件：{PROMPT_PATH.name}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_prompt(question_text: str, extra: str = "") -> tuple[str, str]:
    """→ (system, user)。

    模板**整体**作 system，user 只留一句触发语：图片在 user 消息里，把长篇规则
    放 system 能让模型更稳地遵守格式约束，也让同一份规则在多轮里可被缓存。
    """
    tpl = load_prompt()
    system = (tpl.replace("{{QUESTION_TEXT}}", (question_text or "").strip())
                 .replace("{{ADDITIONAL_REQUIREMENTS}}", (extra or "无").strip()))
    user = "请根据上面的要求和这张图片，输出重绘后的 TikZ 代码。"
    return system, user


def image_refs(body: str) -> list[str]:
    """正文里的图片文件名，按出现顺序。序号即前端的图片下标。"""
    return [m.group(1) for m in _QIMG_RE.finditer(body or "")]


def _local_path(filename: str) -> Path:
    """图片文件名 → 磁盘路径。越界一律拒。

    filename 来自题目正文（正文是用户可编辑的），可能写成 `../../secrets.md`。
    单机版图片扁平存在 `_assets/` 下，所以合法名字里**不该有任何分隔符**；
    再加一道 resolve 后的父目录检查兜住符号链接之类。
    """
    name = (filename or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise RedrawError(f"图片引用不合法：{filename!r}")
    root = config.ASSETS_DIR.resolve()
    p = (root / name).resolve()
    if p.parent != root:
        raise RedrawError(f"图片引用越界：{filename!r}")
    if not p.is_file():
        raise RedrawError(f"找不到图片文件：{name}")
    return p


def _is_generated_name(filename: str) -> bool:
    return bool(_GEN_RE.fullmatch(filename or "")
                or _GEN_IMAGE_RE.fullmatch(filename or ""))


def _save_generated_image(data: bytes) -> str:
    """校验并标准化 Images API 返回的图片，返回内容寻址文件名。"""
    if not data or len(data) > 30 * 1024 * 1024:
        raise RedrawError("图像模型返回的图片为空或超过 30 MB。")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            mode = "RGBA" if "A" in image.getbands() else "RGB"
            normalized = io.BytesIO()
            image.convert(mode).save(normalized, format="PNG")
    except (OSError, UnidentifiedImageError) as e:
        raise RedrawError("图像模型返回的内容不是有效图片。") from e
    content = normalized.getvalue()
    name = f"redraw_{hashlib.sha256(content).hexdigest()[:16]}.png"
    config.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    (config.ASSETS_DIR / name).write_bytes(content)
    return name


def build_image_prompt(question_text: str, extra: str = "") -> str:
    """给 Images Edit 的单一文本提示；图像模型不读取 TikZ 输出约束。"""
    return (
        "请根据输入图片和题干，重绘一张适合中国高中数学或物理试卷的配图。"
        "保留题意、几何关系、物理关系、方向、点名、变量和必要标注；"
        "修正草图中的歪斜、拥挤和不规范布局。使用清晰简约的黑白教材线稿风格，"
        "不要加入题干没有的结论、装饰、彩色、阴影、背景或水印。只返回最终图片。\n\n"
        f"题干：\n{(question_text or '').strip()}\n\n"
        f"补充要求：\n{(extra or '无').strip()}"
    )


def redraw(qid: str, index: int, extra: str = "") -> dict:
    """重绘第 index 张图，**只生成不落库**。→ {"name","svg","pdf","code","old"}

    返回的 name 是新 svg 的文件名，old 是原图文件名；写进正文由 apply_redraw 做，
    这样用户在预览里点"重新生成"可以反复试，正文始终没被动过。
    """
    rec = filestore.get_question(qid)
    if not rec:
        raise RedrawError(f"题目不存在：{qid}")

    refs = image_refs(rec["body"])
    if not refs:
        raise RedrawError("这道题的题干里没有配图。")
    if not (0 <= index < len(refs)):
        raise RedrawError(f"图片下标越界（共 {len(refs)} 张，请求第 {index + 1} 张）。")
    old = refs[index]
    img = _local_path(old)

    if _is_generated_name(old):
        # 已经是上一次重绘的产物。允许再重绘，但始终拿最初的原图作为参考，
        # 避免连续重绘把前一版的误差不断放大。
        orig = filestore.get_img_original(qid, index)
        if not orig:
            raise RedrawError("这张图已经是生成图，没有可供模型参考的原始位图。")
        img = _local_path(orig)

    # 重绘要的是能处理图片的模型，所以走 purpose="redraw"。没单独配时
    # providers.resolve 会回落到「导入识别」那套；聊天视觉模型走 TikZ，图像模型
    # 按模型名自动走 Images Edit。
    provider = providers.resolve("redraw")
    if provider is None:
        raise RedrawError(
            "还没有可用的 LLM 配置。请到「设置 → 识别模型」填一个支持图片输入的"
            "多模态模型（纯文本模型如 deepseek-chat 不行），并勾上「配图重绘」。")

    client = llm_client.build_client(provider)
    try:
        if llm_client.is_image_generation_model(provider.model):
            image_data = client.edit_image(build_image_prompt(rec["body"], extra), img)
            name = _save_generated_image(image_data)
            version = filestore.remember_img_version(
                qid, index, name, kind="generated", model=provider.model,
                prompt=extra)
            logger.info("图像模型重绘成功 qid=%s index=%d → %s", qid, index, name)
            return {"name": name, "svg": name, "pdf": "", "code": "", "old": old,
                    "version": version}

        system, user = build_prompt(rec["body"], extra)
        # 以用户为当前模型设置的值为准。不同视觉模型的上限不同，统一抬高会
        # 让 Qwen 等服务商在请求校验阶段直接拒绝，而不是进入生成流程。
        reply, _finish = client.chat_vision(
            system, user, img, max_tokens=client.max_tokens)
    except llm_client.LLMClientError as e:
        raise RedrawError(f"模型调用失败：{e}") from e

    try:
        code, name_pdf, name_svg = tikz_render.render_from_reply(reply)
    except tikz_render.TikzError as e:
        raise RedrawError(str(e)) from e

    version = filestore.remember_img_version(
        qid, index, name_svg, kind="generated", model=provider.model,
        prompt=extra)
    logger.info("重绘生成成功 qid=%s index=%d → %s", qid, index, name_svg)
    return {"name": name_svg, "svg": name_svg, "pdf": name_pdf,
            "code": code, "old": old, "version": version}


def validate_generated(name: str) -> None:
    """确认这个文件名确实是本模块刚生成的产物。写进正文之前必须过这一关。

    TikZ 产物要求同名 pdf 也在；图像模型产物只要求 PNG 在位。
    """
    n = (name or "").strip()
    if not (_GEN_RE.fullmatch(n) or _GEN_IMAGE_RE.fullmatch(n)):
        raise RedrawError("非法的生成图路径。")
    svg = config.ASSETS_DIR / n
    if not svg.is_file():
        raise RedrawError("生成图片已不存在，请重新生成。")
    if _GEN_RE.fullmatch(n):
        pdf = config.ASSETS_DIR / (n[:-4] + ".pdf")
        if not pdf.is_file():
            raise RedrawError("生成图缺少配套 PDF（导出会掉图），请重新生成。")


def _replace_ref(body: str, index: int, new_name: str) -> tuple[str, str]:
    """把第 index 个图片引用换成 new_name，→ (新正文, 原文件名)。

    按出现序号计数，不用 str.replace：同一文件名在正文里出现两次时 replace 会把
    两处都改掉。宽度后缀（`|60`）一并丢弃——新图的尺寸由 img_layouts 管。
    """
    refs = image_refs(body)
    if not (0 <= index < len(refs)):
        raise RedrawError(f"图片下标越界（共 {len(refs)} 张）。")
    old = refs[index]
    seen = [0]

    def _sub(m):
        cur = seen[0]
        seen[0] += 1
        return f"![[{new_name}]]" if cur == index else m.group(0)

    return _QIMG_RE.sub(_sub, body), old


def apply_redraw(qid: str, index: int, new_name: str) -> str:
    """把重绘结果写进正文，→ 被替换掉的原文件名。

    原图文件不删（内容仍在 `_assets/` 下），只把它的名字记进 img_originals；
    重绘产出的图片同样不删——文件名是内容 hash，属于内容寻址缓存，
    可能被别的题共用。
    """
    validate_generated(new_name)
    rec = filestore.get_question(qid)
    if not rec:
        raise RedrawError(f"题目不存在：{qid}")

    body, old = _replace_ref(rec["body"], index, new_name)
    filestore.update_question(qid, body, rec["solution"], rec["type"],
                              rec["source"], rec["difficulty"], rec["tags"])
    # 预览任务会提前登记版本；直接应用一个仍在资产目录中的生成物时也补登记。
    filestore.remember_img_version(qid, index, new_name, kind="generated")
    # 首次写入即锁定原图（见 filestore.remember_img_original 的注释）
    filestore.remember_img_original(qid, index, old)
    logger.info("重绘已应用 qid=%s index=%d %s → %s", qid, index, old, new_name)
    return old


def switch_version(qid: str, index: int, name: str) -> tuple[str, str]:
    """把正文第 index 张图切换到已登记的版本，返回 (新文件名, 旧文件名)。"""
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise RedrawError("图片版本文件名不合法。")
    rec = filestore.get_question(qid)
    if not rec:
        raise RedrawError(f"题目不存在：{qid}")
    refs = image_refs(rec["body"])
    if not (0 <= index < len(refs)):
        raise RedrawError(f"图片下标越界（共 {len(refs)} 张，请求第 {index + 1} 张）。")
    versions = filestore.ensure_img_versions(qid)
    target = next((item for item in versions
                   if item.get("i") == index and item.get("name") == name), None)
    if target is None:
        raise RedrawError("图片版本不存在或已被移除。")
    _local_path(name)
    body, old = _replace_ref(rec["body"], index, name)
    if body != rec["body"]:
        filestore.update_question(qid, body, rec["solution"], rec["type"],
                                  rec["source"], rec["difficulty"], rec["tags"])
    logger.info("图片版本已切换 qid=%s index=%d %s → %s", qid, index, old, name)
    return name, old


def restore_original(qid: str, index: int) -> tuple[str, str]:
    """退回原图，→ (原文件名, 被换下的生成图名)。

    只收 index，**不接受客户端传路径**——原文件名一律从 frontmatter 读，否则
    等于允许把任意字符串写进正文。
    """
    orig = filestore.get_img_original(qid, index)
    if not orig:
        raise RedrawError("这张图没有可还原的原图记录。")
    _local_path(orig)          # 原图真的还在盘上才动正文
    _new, cur = switch_version(qid, index, orig)
    # 还原后删记录：否则"还原原图"按钮一直亮着，点了却没有任何变化，像坏了一样。
    filestore.forget_img_original(qid, index)
    logger.info("已还原原图 qid=%s index=%d %s → %s", qid, index, cur, orig)
    return orig, cur
