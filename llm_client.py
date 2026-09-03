"""OpenAI 兼容的 LLM 客户端，用于文本规范化和配图重绘。

为什么不直接用 project-alpha 的 `src/deepseek_client.py`：
  1. 它的 BASE_URL 是硬编码的类属性，换不了服务商；
  2. 它把 max_tokens=8192 写死在 `chat()` 的默认参数上，而 project-alpha 的
     normalizer 调用时只传两个位置参数，所以那个默认值就是实际生效值——
     换成 deepseek-v4-pro 这类**推理模型**后，reasoning_content 与 content
     共享同一份 max_tokens 预算，长草稿的思维链吃光 8192，content 返回空串，
     于是抛「DeepSeek 返回空内容」（这正是本功能要修的线上故障）。
而 project-alpha 是「不改一行」的既有约定，所以在 QuizForge 侧自己实现一份，
接口与 DeepSeekClient 保持一致（鸭子类型），可直接喂给 `src.normalizer.normalize`：

    chat(system_prompt, user_content) -> (content, finish_reason)

normalize() 靠 finish_reason == "length" 判断被截断并续传，所以 finish_reason
必须原样返回，不能吞掉。

唯一的例外是「假完成」：模型在长草稿上只输出前几题就自己收尾，服务端给
finish_reason="stop"。此时 normalize() 认定「正常结束」直接 break，一轮续传都
不会发生，后面的题被静默丢弃（实测 claude-haiku-4-5 在 43K 字符的题干+解析
合并草稿上只吐 11/19 题就 stop）。见 _looks_truncated()。
"""

import base64
import json
import ipaddress
import logging
import re
import socket
import unicodedata
import urllib.parse
from pathlib import Path

import httpx
from openai import OpenAI
from httpcore._backends.sync import SyncBackend

logger = logging.getLogger(__name__)

# max_tokens 合法区间。上限取一个宽松值：实测 deepseek-v4-pro 接受到 131072，
# 别家模型只会更小——填超了由服务商自己报错，我们不替它猜。
MAX_TOKENS_MIN = 256
MAX_TOKENS_MAX = 200000
MAX_TOKENS_DEFAULT = 8192


def is_image_generation_model(model: str) -> bool:
    """判断是否应使用 Images Edit，而不是 Chat Completions。"""
    value = (model or "").strip().lower()
    return bool(re.search(r"(?:^|[/_-])gpt-image(?:[/_-]|$)", value))

# 草稿里的题号：行首（允许前置 markdown 标题记号/空格）的 1~2 位数字 + 中英文点号。
# MinerU 有时把大题写成 "## 17. (15 分)"，所以 #* 必须允许。
_DRAFT_NUM_RE = re.compile(r"^\s*#{0,4}\s*(\d{1,2})\s*[.．、]", re.MULTILINE)
# 输出里的题块：normalizer 要求每题以 "- " 开头（顶格，非缩进的续行）
_OUT_BLOCK_RE = re.compile(r"^- ", re.MULTILINE)
# 续传轮 user_content 里的「已完成清单」正文（normalizer 每行写成 "N. 题干摘要"）
_DONE_LIST_RE = re.compile(r"<已完成清单>(.*?)</已完成清单>", re.S)
_DONE_ITEM_RE = re.compile(r"^\s*\d+\.", re.MULTILINE)


class BaseURLSecurityError(ValueError):
    """Base URL 可能把服务器请求引向内网或泄露 API Key。"""


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """只允许普通公网单播地址；保留段、组播与 IPv4-mapped IPv6 都从严处理。"""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return _is_public_address(mapped)
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def resolve_public_ips(host: str, port: int = 443) -> tuple[str, ...]:
    """解析主机并返回经过 SSRF 边界检查的公网 IP。

    只要 DNS 同时返回任一内网/本机/保留地址就整体拒绝，不能挑一个公网地址放行：
    攻击者可以让记录轮转，保存时给公网、真正请求时再给 127.0.0.1。
    """
    try:
        literal = ipaddress.ip_address(host.rstrip("."))
        addresses = [literal]
    except ValueError:
        try:
            rows = socket.getaddrinfo(
                host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise BaseURLSecurityError(f"Base URL 的主机无法解析：{host}") from exc
        addresses = []
        seen = set()
        for row in rows:
            value = row[4][0]
            address = ipaddress.ip_address(value)
            if address not in seen:
                seen.add(address)
                addresses.append(address)
    if not addresses:
        raise BaseURLSecurityError(f"Base URL 的主机没有可用地址：{host}")
    blocked = [str(address) for address in addresses if not _is_public_address(address)]
    if blocked:
        raise BaseURLSecurityError(
            "Base URL 只能指向公网 API，不能访问本机、局域网、云元数据或保留地址"
        )
    # 服务器未必有 IPv6 出口，优先 IPv4；Host 与 TLS SNI 仍使用原域名。
    addresses.sort(key=lambda address: address.version)
    return tuple(str(address) for address in addresses)


def _is_loopback_url(parsed) -> bool:
    """判断是否指向本机回环地址。

    单机版特有：本地推理服务（Ollama、LM Studio、vLLM 等）就跑在 127.0.0.1 上，
    且只有 http。服务器版禁止内网地址是为了防止**它的用户**拿服务器去探内网，
    而单机版的「用户」就是本机主人，自己连自己的服务不构成越权，也不存在
    API Key 出机器的问题（流量根本没离开 loopback）。
    """
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(url: str) -> str:
    """规范化并验证用户可控的 OpenAI 兼容 Base URL。

    强制 HTTPS 防止 API Key 明文外泄；保存时解析一次，真正建连时还会再次解析并
    把 socket 固定到已验证的 IP，避免 DNS rebinding 的检查/使用时间差。

    单机版例外：放行 http://127.0.0.1 这类回环地址，供本地推理服务使用
    （见 _is_loopback_url）。
    """
    normalized = normalize_base_url(url)
    if not normalized or any(
        ord(char) < 33 or char.isspace() or unicodedata.category(char) == "Cf"
        for char in normalized
    ):
        raise BaseURLSecurityError("Base URL 格式无效")
    try:
        parsed = urllib.parse.urlsplit(normalized)
        port = parsed.port or 443
    except ValueError as exc:
        raise BaseURLSecurityError("Base URL 端口无效") from exc
    if not parsed.hostname:
        raise BaseURLSecurityError("Base URL 缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise BaseURLSecurityError("Base URL 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise BaseURLSecurityError("Base URL 不能包含查询参数或片段")
    # 本地推理服务：跳过 HTTPS 与公网地址两项检查（流量不出本机）
    if _is_loopback_url(parsed):
        return normalized
    if parsed.scheme.lower() != "https":
        raise BaseURLSecurityError("Base URL 必须使用 HTTPS，避免 API Key 明文传输")
    resolve_public_ips(parsed.hostname, port)
    return normalized


class _PublicOnlyNetworkBackend(SyncBackend):
    """在真正 TCP 建连前复验 DNS，并连接已验证的数字 IP。

    httpcore 后续仍用原始 hostname 做 Host 与 TLS SNI；这里只有 socket 目标被固定，
    因而既保留正常证书校验，也堵住“校验后 DNS 立刻改指内网”的 rebinding 窗口。
    """

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        # 回环地址直连：与 validate_base_url 的放行口径一致，否则本地推理服务
        # 能存进设置却连不上（保存时放行、建连时拒绝）。
        try:
            if ipaddress.ip_address(host).is_loopback:
                return super().connect_tcp(
                    host, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
        except ValueError:
            if host.lower() in ("localhost", "::1"):
                return super().connect_tcp(
                    host, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
        address = resolve_public_ips(host, port)[0]
        return super().connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def _safe_http_client() -> httpx.Client:
    """为 OpenAI SDK 建立不读系统代理、禁止重定向、DNS 建连受控的客户端。"""
    transport = httpx.HTTPTransport(trust_env=False)
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError("当前 httpx/httpcore 版本不支持安全网络后端")
    pool._network_backend = _PublicOnlyNetworkBackend()
    return httpx.Client(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
    )


def count_draft_questions(user_content: str) -> int:
    """数一数草稿里大约有多少道题，用于判断模型是否提前收尾。

    只取 `<草稿>`/`<原始草稿>` 标签内的正文，并在「参考答案与解析」处截断——
    双文件模式下解析区会把 1..N 的题号再数一遍，不截断会得到约两倍的题数。
    题号可能不从 1 开始也可能有跳号，所以取「不同题号的个数」而不是最大值。
    """
    m = re.search(r"<(?:原始)?草稿>(.*)</(?:原始)?草稿>", user_content, re.S)
    body = m.group(1) if m else user_content
    head = re.split(r"#\s*参考答案与解析", body, maxsplit=1)[0]
    return len({int(x) for x in _DRAFT_NUM_RE.findall(head)})


def count_done_questions(user_content: str) -> int:
    """续传轮「已完成清单」里列了多少题；首轮没有该清单，返回 0。"""
    m = _DONE_LIST_RE.search(user_content)
    return len(_DONE_ITEM_RE.findall(m.group(1))) if m else 0


def _looks_truncated(content: str, user_content: str, finish: str,
                     system_prompt: str = "") -> bool:
    """模型是否「假装说完了」——服务端报 stop，但累计题数明显少于草稿。

    normalize() 只在 finish_reason == "length" 时续传。模型在长草稿上偷懒、
    只输出前几题就收尾时服务端给的是 stop，于是续传不触发、后面的题被静默丢掉。
    这里检出这种情况并对 normalize() 报 "length"，让它既有的续传逻辑接管：
    下一轮会带上「已完成清单」要求模型继续输出其余题。

    续传轮同样要判——那一轮模型也可能再次提前收尾（实测 claude-haiku-4-5 在
    43K 合并草稿上第 1 轮出 10 题、第 2 轮再出 8 题就 stop，卡在 17/19）。所以
    比较的是「已完成清单里的题数 + 本轮输出题数」对草稿题数，而不是只看本轮。
    注意 include_solution 时「已完成」只算带解析的块，缺解析的题会被要求重出，
    于是 done + got 可能超过 want，那属于正常情况、不触发。

    容差取「至少差 2 题」：模型合并小问、或我方题号正则多认了一两个（如解析里
    的分点编号）都属常见噪声，留出余量避免无谓地多跑一轮。死循环由 normalize()
    既有的 stale_rounds（连续 2 轮无进展）和 max_rounds 兜住。
    只处理 finish == "stop"；length 本来就会续传，其余（content_filter 等）不碰。

    只规范化指定题号（normalizer 的 only_numbers）时，输出本就该远少于草稿，
    一律不判——靠 system prompt 里那条「只输出题号为 …」标记识别。
    """
    if finish != "stop":
        return False
    if "只输出题号为" in system_prompt:
        return False
    want = count_draft_questions(user_content)
    if want < 3:
        return False  # 草稿本来就没几题，数不准，不猜
    got = len(_OUT_BLOCK_RE.findall(content)) + count_done_questions(user_content)
    return got + 2 <= want


def normalize_base_url(url: str) -> str:
    """把用户填的 Base URL 补成 openai SDK 需要的形式。

    openai SDK 只在 base_url 后面接 `/chat/completions`，不会替你补 `/v1`。
    而 CC Switch 那类工具用的是 Anthropic 官方 SDK，它自己会补 `/v1/messages`，
    所以中转站地址在那边填裸域名就能用、填到这里却打到官网首页上（返回 HTML）。
    这个差异不该让用户去记，这里统一补齐：

        https://yapi.click            -> https://yapi.click/v1
        https://yapi.click/           -> https://yapi.click/v1
        https://yapi.click/v1         -> 原样
        https://api.deepseek.com      -> 原样（DeepSeek 自己就挂在根路径）
        .../v1/chat/completions       -> 砍掉后缀，回到 .../v1

    已经带路径（/v1、/api、/openai 之类）的一律不动，免得把人家的自定义前缀改坏。
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return u
    # 误填成完整端点时，砍回根路径
    for suffix in ("/chat/completions", "/completions"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
            break
    parsed = urllib.parse.urlsplit(u)
    path = parsed.path.strip("/")
    if path:
        return u  # 已有路径前缀，尊重用户填的
    # 裸域名：DeepSeek 这类把 API 挂在根上的不能加 /v1，其余按 OpenAI 惯例补
    host = parsed.netloc.lower()
    if host.endswith("api.deepseek.com"):
        return u
    return u + "/v1"


def _parse_sse(body: str) -> tuple[str, str]:
    """把 SSE 事件流（`data: {...}` 逐行）拼回 (content, finish_reason)。

    某些中转站无视 stream=False 一律回事件流，openai SDK 不认、原样把文本给我们。
    只取 `delta.content`（`reasoning_content` 是思维链，不是正文，不能拼进来）。
    `data: [DONE]` 与解析不了的行直接跳过——半行截断在流被掐断时很常见，
    这时宁可少一段也别整体失败；真丢了内容会由题数校验判成截断去续传。
    """
    parts: list[str] = []
    finish = ""
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        for ch in chunk.get("choices") or []:
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                parts.append(piece)
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    return "".join(parts), finish or "stop"


class LLMClientError(Exception):
    """LLM 调用失败。converter.py 会原样包成 ConvertError 透给用户，
    所以 message 要写成用户看得懂、知道下一步该改什么的话。"""


def _message_text(content) -> str:
    """兼容字符串和部分中转站返回的内容片段数组。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        else:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def clamp_max_tokens(value, default: int = MAX_TOKENS_DEFAULT) -> int:
    """表单里填的 max_tokens 归一化成合法整数（非数字/越界都回退到区间内）。"""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(MAX_TOKENS_MIN, min(MAX_TOKENS_MAX, n))


class LLMClient:
    """任意 OpenAI 兼容服务的 chat completions 客户端。

    max_tokens 存在实例上、并作为 `chat()` 的兜底值——这样 normalizer 那句
    不带 max_tokens 的 `client.chat(system, user)` 也能吃到用户配置的额度，
    推理模型只要把它调大（32000+）就不会再被思维链挤空。
    """

    def __init__(self, api_key: str, base_url: str, model: str,
                 max_tokens: int = MAX_TOKENS_DEFAULT):
        self.model = model
        self.base_url = validate_base_url(base_url)
        self.max_tokens = clamp_max_tokens(max_tokens)
        if self.base_url != (base_url or "").strip().rstrip("/"):
            logger.info("Base URL 已补全: %s -> %s", base_url, self.base_url)
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            http_client=_safe_http_client(),
        )

    def chat(self, system_prompt: str, user_content: str,
             temperature: float = 0.1, max_tokens: int | None = None) -> tuple[str, str]:
        """返回 (content, finish_reason)。失败抛 LLMClientError。"""
        mt = self.max_tokens if max_tokens is None else clamp_max_tokens(max_tokens)
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=temperature,
                max_tokens=mt,
            )
        except Exception as e:
            detail = " ".join(str(e).split())
            # base_url 填错时服务端常回一整页 HTML，SDK 把它整段塞进异常 message
            # （实测 example.com 返回 405 + 558 字符 HTML）。提示必须放在截断之前，
            # 否则被 HTML 挤到看不见。
            if "<html" in detail.lower() or "<!doctype" in detail.lower():
                raise LLMClientError(
                    f"Base URL（{self.base_url}）指向的是一个网页而不是 API："
                    f"服务端返回了 HTML。请填 API 根路径，例如 https://api.deepseek.com 或 "
                    f"https://api.anthropic.com/v1/，不要带 /chat/completions 后缀。"
                    f"（HTTP {getattr(e, 'status_code', '?')}）"
                ) from e
            raise LLMClientError(
                f"LLM 调用失败（{self.model} @ {self.base_url}）: {detail[:300]}"
            ) from e

        # HTTP 200 但 body 不是 JSON 时，openai SDK 不解析、直接把响应体原文当 str
        # 返回（见 openai/_response.py 的 "responds with content that isn't JSON"
        # 分支）。最常见的是 base_url 填错落到了返回 HTML 的页面、或被网关/反代
        # 拦了。这里必须挡住，否则下一行取 .choices 会抛 AttributeError，被
        # converter 的兜底 handler 包成看不出原因的报错。
        if not hasattr(resp, "choices"):
            body = resp if isinstance(resp, str) else repr(resp)
            # 例外：有的中转站对某些模型**无视 stream=False**，一律回 SSE 事件流
            # （实测 yapi.click 的 deepseek-v4-pro 就这样）。这不是配置错误，
            # 而是服务端行为，自己把事件流拼回来即可，不该报「Base URL 填错」。
            if body.lstrip().startswith("data:"):
                content, finish = _parse_sse(body)
                logger.info("服务端强制流式返回，已按 SSE 解析: model=%s len=%d finish=%s",
                            self.model, len(content), finish)
                return self._finalize(content, user_content, finish, system_prompt, mt)
            snippet = " ".join(body.split())[:200]
            logger.warning("响应不是 chat completions JSON: model=%s base_url=%s body=%.500s",
                           self.model, self.base_url, body)
            raise LLMClientError(
                f"{self.base_url} 返回的不是 OpenAI 兼容的 chat completions 响应"
                f"（拿到一段非 JSON 文本）。多半是 Base URL 填错了——请确认它指向 API 根路径"
                f"（如 https://api.deepseek.com 或 https://api.anthropic.com/v1/），"
                f"不要带 /chat/completions 后缀。响应开头：{snippet}"
            )

        choices = resp.choices or []
        if not choices:
            raise LLMClientError(
                f"模型 {self.model} 的响应里没有 choices（服务端返回异常）。"
                f"请检查 Base URL 与模型名是否匹配。"
            )
        choice = choices[0]
        return self._finalize(_message_text(choice.message.content), user_content,
                              choice.finish_reason, system_prompt, mt,
                              getattr(choice.message, "reasoning_content", None))

    def chat_vision(self, system_prompt: str, user_content: str,
                    image_path, temperature: float = 0.2,
                    max_tokens: int | None = None) -> tuple[str, str]:
        """带图对话（AI 重绘配图用）。返回 (content, finish_reason)。

        为什么是新方法而不是给 `chat()` 加个 image 参数：`chat()` 的签名是与
        project-alpha 的 `DeepSeekClient` 对齐的鸭子类型契约，会被原样喂给
        `src.normalizer.normalize`（那边按位置传两个参数）。动它的签名等于动一条
        跑在生产上的链路，而 project-alpha 是「不改一行」的约定。

        走 OpenAI 兼容的多模态形式：user.content 是数组，图片用 base64 data URL
        的 `image_url`。注意不是所有模型都支持——纯文本模型（如 deepseek-chat）
        会直接报错，故错误提示里点明「需要多模态模型」，否则用户只看到一句
        看不懂的 400。

        默认 temperature 比 chat() 的 0.1 略高（0.2）：绘图要一点构图上的灵活性，
        但仍需足够确定性来遵守输出格式约束。
        """
        import base64
        import mimetypes

        p = Path(image_path)
        if not p.is_file():
            raise LLMClientError(f"找不到要重绘的图片：{p.name}")
        mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        try:
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        except OSError as e:
            raise LLMClientError(f"读取图片失败：{e}") from e

        mt = self.max_tokens if max_tokens is None else clamp_max_tokens(max_tokens)
        # 图片放在文字之前：多数视觉模型对「先看图再读要求」的顺序响应更好，
        # 也与 Anthropic 官方文档给的示例顺序一致。
        parts = [
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": user_content},
        ]
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": parts},
                ],
                temperature=temperature,
                max_tokens=mt,
            )
        except Exception as e:
            detail = " ".join(str(e).split())
            raise LLMClientError(
                f"带图调用失败（{self.model} @ {self.base_url}）："
                f"{detail[:300]}。请确认该模型支持图片输入"
                f"（纯文本模型如 deepseek-chat 不支持，需换成多模态模型）。"
            ) from e

        if not hasattr(resp, "choices"):
            body = resp if isinstance(resp, str) else repr(resp)
            if body.lstrip().startswith("data:"):
                content, finish = _parse_sse(body)
                return self._finalize_plain(content, finish, mt)
            snippet = " ".join(body.split())[:200]
            raise LLMClientError(
                f"{self.base_url} 返回的不是 OpenAI 兼容响应。响应开头：{snippet}")

        choices = resp.choices or []
        if not choices:
            raise LLMClientError(f"模型 {self.model} 的响应里没有 choices。")
        choice = choices[0]
        return self._finalize_plain(
            _message_text(choice.message.content), choice.finish_reason, mt,
            getattr(choice.message, "reasoning_content", None))

    def edit_image(self, prompt: str, image_path) -> bytes:
        """通过 OpenAI Images Edit 生成一张重绘后的位图。"""
        import mimetypes

        p = Path(image_path)
        if not p.is_file():
            raise LLMClientError(f"找不到要重绘的图片：{p.name}")
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        if not mime.startswith("image/"):
            mime = "image/png"
        try:
            image_data = p.read_bytes()
        except OSError as e:
            raise LLMClientError(f"读取图片失败：{e}") from e

        try:
            response = self._client.images.edit(
                model=self.model,
                image=(p.name, image_data, mime),
                prompt=prompt,
            )
        except Exception as e:
            detail = " ".join(str(e).split())
            raise LLMClientError(
                f"图像编辑调用失败（{self.model} @ {self.base_url}）："
                f"{detail[:300]}。请确认 YAPI 已提供 Images Edit 接口，"
                f"且模型名是支持图片编辑的 gpt-image-* 模型。"
            ) from e

        items = getattr(response, "data", None) or []
        if not items:
            raise LLMClientError(f"图像模型 {self.model} 的响应里没有图片数据。")
        item = items[0]
        encoded = getattr(item, "b64_json", None)
        if encoded:
            try:
                if encoded.startswith("data:"):
                    encoded = encoded.split(",", 1)[1]
                return base64.b64decode(encoded, validate=True)
            except (ValueError, UnicodeEncodeError) as e:
                raise LLMClientError(
                    f"图像模型 {self.model} 返回的 Base64 图片无效。"
                ) from e

        image_url = getattr(item, "url", None)
        if image_url:
            try:
                with _safe_http_client() as client:
                    downloaded = client.get(image_url)
                    downloaded.raise_for_status()
                    return downloaded.content
            except Exception as e:
                raise LLMClientError(
                    f"图像模型 {self.model} 返回的图片地址无法下载：{e}"
                ) from e
        raise LLMClientError(
            f"图像模型 {self.model} 的响应既没有 b64_json，也没有图片地址。"
        )

    def _finalize_plain(self, content, finish: str, mt: int,
                        reasoning: str | None = None) -> tuple[str, str]:
        """空内容判错，但**不做**「假完成」题数校验。

        `_finalize` 那套是给 md 规范化用的（数草稿题数判断模型是否偷懒），
        配图生成只出一段 TikZ，没有「题数」概念，套上去会误判。
        """
        if not content:
            if finish == "length":
                raise LLMClientError(
                    f"模型 {self.model} 的思维链占满了 max_tokens 预算（当前 {mt}），"
                    f"没有输出正文。请在 LLM Provider 配置里把 max_tokens 调大"
                    f"（建议 16000 以上）后重试。")
            raise LLMClientError(f"模型 {self.model} 返回空内容（finish_reason={finish}）")
        return content.strip(), finish

    def _finalize(self, content, user_content: str, finish: str,
                  system_prompt: str, mt: int,
                  reasoning: str | None = None) -> tuple[str, str]:
        """空内容判错 + 「假完成」改报 length。非流式与 SSE 两条路共用。"""
        if not content:
            # 推理模型的典型故障：思维链把预算吃光，可见回答一个字都没吐出来。
            # 报清楚「调大 max_tokens」，而不是笼统的「返回空内容」。
            if finish == "length":
                logger.warning("空内容且被截断: model=%s max_tokens=%s reasoning_len=%d",
                               self.model, mt, len(reasoning or ""))
                raise LLMClientError(
                    f"模型 {self.model} 的思维链占满了 max_tokens 预算（当前 {mt}），"
                    f"没有输出正文。请在 LLM Provider 配置里把 max_tokens 调大"
                    f"（推理模型建议 32000 以上），或改用非推理模型后重试。"
                )
            raise LLMClientError(f"模型 {self.model} 返回空内容（finish_reason={finish}）")

        content = content.strip()
        # 「假完成」兜底：服务端报 stop 但明显没输出完，改报 length 让 normalize()
        # 走续传。不这么做的话后面的题会被静默丢弃、用户只看到少了一半的结果。
        if _looks_truncated(content, user_content, finish, system_prompt):
            logger.warning(
                "模型提前收尾（草稿约 %d 题，本轮输出 %d 题 + 已完成 %d 题，"
                "finish_reason=stop），按截断处理以触发续传: model=%s",
                count_draft_questions(user_content),
                len(_OUT_BLOCK_RE.findall(content)),
                count_done_questions(user_content), self.model)
            finish = "length"

        return content, finish


def build_client(provider) -> LLMClient:
    """由 llm_provider.ProviderConfig 造一个客户端。"""
    return LLMClient(provider.api_key, provider.base_url,
                     provider.model, provider.max_tokens)
