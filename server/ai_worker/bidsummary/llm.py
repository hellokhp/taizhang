"""LLM 统一封装（OpenAI 兼容协议）。

只暴露三个能力：
  * chat_json(prompt)       —— 文本 -> 结构化 JSON（带 system 角色设定）
  * vision_json(image, prompt) —— 图片 -> 结构化 JSON（公示截图/扫描表格专用）
  * vision_markdown(bytes)  —— 扫描页/图片 -> Markdown
所有重试、容错、Token 统计都在这里收口。

模型供应商切换：在 .env / supervisor environment 设 LLM_PROVIDER 即可（默认 dashscope=千问）。
文本与视觉可分别指定供应商（典型：DeepSeek 跑文本 + 千问 VL 跑视觉）。
详见 bidsummary/config.py 的 PROVIDERS 注册表。

提示词约定（为什么每个调用都要传 system）：
  qwen 系列对 system 消息的角色设定遵循度显著高于 user 消息内的指令，
  把「你是谁 + 只依据原文 + 只输出 JSON」放进 system，把易变的具体任务
  放进 user，可以把幻觉率压得更低。
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from openai import OpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from bidsummary.config import CONFIG

_client: OpenAI | None = None  # 已弃用，保留避免其它模块遗留引用
_text_client: OpenAI | None = None
_vision_client: OpenAI | None = None
_usage = {"in": 0, "out": 0}

# ------------------------------------------------------------------ 系统 prompt
SYS_EXTRACTOR = (
    "你是中国招投标领域的资深开标信息抽取专家，熟悉《招标投标法》《政府采购法》"
    "及各金融机构、国企的采购评标惯例。\n"
    "工作铁律：\n"
    "1. 只依据【资料】里出现的原文作答；原文没有的信息一律返回 null，"
    "严禁推测、补全或使用任何外部知识。\n"
    "2. 所有数字（金额、日期、编号、税率）必须与原文逐字一致，禁止任何格式化改写或单位换算"
    "（换算由外部代码完成）。\n"
    "3. 只输出一个合法的 JSON 对象：不以```开头、不含任何解释文字、不含注释。"
)

SYS_RULE_PARSER = (
    "你是招投标评标办法解析专家，专长是把评标办法章节中的「价格分计算规则」"
    "翻译成可编程执行的参数。\n"
    "工作铁律：\n"
    "1. 只输出原文明确写出的规则参数；原文没写的字段返回 null，严禁按行业惯例脑补。\n"
    "2. evidence_* 字段必须是招标文件中逐字照抄的原句（标点也一致），"
    "用于程序回原文核对；找不到原句就返回空字符串，严禁自己组织语言。\n"
    "3. 只输出一个合法的 JSON 对象，不含任何解释文字。"
)

SYS_PRICE_REVIEW_PARSER = (
    "你是中国招投标「下浮率 / 折扣率报价」类项目的价格评审规则解析专家，"
    "专长是把招标文件里散落在多页的价格条款翻译成可编程执行的参数。\n"
    "工作铁律：\n"
    "1. 只依据【资料】中的原文作答。资料按 <!-- page:N --> 分页，"
    "请在答案里带上证据所在页码；原文没写的字段一律返回 null，严禁按行业惯例脑补。\n"
    "2. 必须分清三种「百分比」，三者绝不可混用：\n"
    "   ① 下浮率 / 优惠率：报价本身（如 7.86%），此时折扣 = 1 − 下浮率/100；\n"
    "   ② 折扣率报价：报价本身就是评审价（如「市场价格的 67%」），不得再拿 1 去减；\n"
    "   ③ 相对偏差百分点：「每高于/低于评标基准价 1% 加/扣 X 分」里的 1% 指 "
    "(报价−基准价)/基准价×100，是得分步长，不是报价，也不是下浮率。\n"
    "   金额/单价换算一律由外部代码完成，你只回报规则。\n"
    "3. 必须区分「基准价取法」（平均值 / 最低值 / 超过 N 家去高去低后平均）与"
    "「得分公式」。得分公式有两种形态，务必先判定是哪一种：\n"
    "   · 满分扣分型：等于基准价得满分，高于每 1% 扣 X 分、低于每 1% 扣 Y 分；\n"
    "   · 基本分增减分型：等于基准价得基本分，低于每 1% 加 X 分至满分封顶，"
    "低于超过某个幅度后改为在满分基础上扣 X 分，高于每 1% 扣 X 分。\n"
    "   两套公式的参数结构不同，不要把「基本分」当成「满分」，也不要把「加分」当成「扣分」。\n"
    "4. evidence_* 字段必须是逐字照抄的原文原句（含标点，可省略中间未涉及的无关段落，"
    "但不得改写、不得用省略号拼接、不得自己组织语言）；找不到原句就返回空字符串。\n"
    "5. 只输出一个合法的 JSON 对象，不含任何解释文字。"
)

SYS_SHEET_BIDDER_JUDGE = (
    "你是招投标文件版式识别专家，专长判断「Excel 工作簿里哪些 sheet 代表投标人」。\n"
    "真实项目里投标人不一定写在单元格中：常见做法是**每个 sheet 以投标人名称（或公司简称）命名**，"
    "sheet 内部是该投标人的产品单价明细。也可能混有「汇总」「说明」「封面」「目录」「Sheet1」"
    "等非投标人页。\n"
    "工作铁律：\n"
    "1. 只根据给出的 sheet 名称判断，不要臆造公司全称、不要补全地域或「有限公司」后缀；"
    "输出的名称必须与输入逐字一致。\n"
    "2. 名称是地域+字号/行业词（如「北京金车光电」「杭州柏年智能光电子」「沈阳诚邦标识」）、"
    "或含「公司/集团/厂/中心」的，判为投标人；纯序号、Sheet、汇总、说明、模板、目录判为非投标人。\n"
    "3. 拿不准的归入 bidders 并给出 reason，由后续结构校验（表头/产品行数）兜底，不要自行删减。\n"
    "4. 只输出一个合法的 JSON 对象，不含任何解释文字。"
)

SYS_PRICE_RULE_GENERAL = (
    "你是中国招投标「评标办法 / 价格评分标准」解析专家，专长是把写在表格单元格、"
    "章节正文、附件说明里的价格条款还原成可编程执行的参数。\n"
    "工作铁律：\n"
    "1. 只依据【资料】原文作答；原文没写的字段一律返回 null，严禁按行业惯例脑补公式或分值。\n"
    "2. 价格条款的写法很多，不要只认「投标报价评分标准」这个标题："
    "「价格部分（50分）」「价格总分50分」「最低价法」「分项价格分=50*分项权重」"
    "「每项标的得分=（所有投标人中最低价格/投标人投标价格）×分项价格分」"
    "「X/Y＜0.5 该项不得分」「税率相同时按含税价计算」等都可能是关键条款，"
    "它们可能出现在评分表的某一行单元格里。\n"
    "3. 必须区分：整包满分、分项权重、每项得分公式、偏离惩罚阈值、计价口径（含税/不含税），"
    "分别填到对应字段，不要合并成一句自然语言。\n"
    "4. evidence 必须是逐字照抄的原文原句（含标点），供程序回原文核对；"
    "找不到原句就返回空字符串，严禁自己组织语言或改写数字。\n"
    "5. 只输出一个合法的 JSON 对象，不含任何解释文字。"
)

SYS_VISION_TRANSCRIBE = (
    "你是招投标文件扫描件的忠实转写员。\n"
    "铁律：只转写图中真实存在的内容，禁止猜测、总结、补全；看不清的字用 □ 占位。"
)

SYS_VISION_EXTRACTOR = (
    "你是招投标公示/公告截图的信息抽取专家。\n"
    "铁律：只依据图片中真实可见的文字作答；看不清、看不到的信息返回 null，"
    "严禁按常见格式推测；公司名称必须与图中文字逐字一致。只输出一个合法的 JSON 对象。"
)

# ------------------------------------------------------------------ 提示词外置钩子（台账接入 §14）
# 独立 CLI 不设 hook => 行为与原始工具完全一致；
# Worker 启动时 set_prompt_hook(...) 注入 DB active 版本 + 字段契约 + few-shot。
SYS_TO_SCENE = {
    SYS_EXTRACTOR: 'extractor',
    SYS_RULE_PARSER: 'price_rule',
    SYS_PRICE_REVIEW_PARSER: 'price_review',
    SYS_SHEET_BIDDER_JUDGE: 'sheet_judge',
    SYS_PRICE_RULE_GENERAL: 'price_rule_general',
    SYS_VISION_TRANSCRIBE: 'vision_transcribe',
    SYS_VISION_EXTRACTOR: 'vision',
}
_PROMPT_HOOK = None


def set_prompt_hook(fn):
    """fn(scene, base_system, user_prompt) -> (system, prompt)；传 None 关闭。"""
    global _PROMPT_HOOK
    _PROMPT_HOOK = fn


def _apply(scene, system, prompt):
    if _PROMPT_HOOK is None:
        return system, prompt
    try:
        out = _PROMPT_HOOK(scene, system, prompt)
        if isinstance(out, tuple) and len(out) == 2:
            return out[0] or system, out[1] or prompt
    except Exception as e:
        print('[提示词] 外置版本加载失败，回退代码常量：%s' % str(e)[:120])
    return system, prompt


def text_client() -> OpenAI:
    """文本/JSON 抽取走这个客户端；供应商由 LLM_PROVIDER 决定。"""
    global _text_client
    if _text_client is None:
        if not CONFIG.text_base_url or not CONFIG.text_api_key:
            raise RuntimeError(
                "未配置文本模型连接。请在 .env / supervisor environment 设置 LLM_PROVIDER 对应的\n"
                "密钥（DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / WORKBUDDY_API_KEY 等）与 LLM_BASE_URL。"
            )
        _text_client = OpenAI(
            api_key=CONFIG.text_api_key,
            base_url=CONFIG.text_base_url.rstrip("/"),
            timeout=CONFIG.request_timeout,
            max_retries=0,
        )
    return _text_client


def vision_client() -> OpenAI:
    """视觉模型（公示截图 / 扫描转写）走这个客户端；供应商由 LLM_VISION_PROVIDER 决定。"""
    global _vision_client
    if not CONFIG.vision_available:
        raise RuntimeError(
            "当前视觉模型未配置：所选供应商无视觉能力，或未设置 LLM_VISION_MODEL。\n"
            "公示截图识别 / 扫描页转写 需要视觉模型。请设置 LLM_VISION_PROVIDER=dashscope，\n"
            "或显式填写 LLM_VISION_BASE_URL / LLM_VISION_API_KEY / LLM_VISION_MODEL。"
        )
    if _vision_client is None:
        _vision_client = OpenAI(
            api_key=CONFIG.vision_api_key,
            base_url=CONFIG.vision_base_url.rstrip("/"),
            timeout=CONFIG.request_timeout,
            max_retries=0,
        )
    return _vision_client


def usage() -> dict[str, int]:
    return dict(_usage)


def _record(resp: Any) -> None:
    try:
        u = resp.usage
        _usage["in"] += getattr(u, "prompt_tokens", 0) or 0
        _usage["out"] += getattr(u, "completion_tokens", 0) or 0
    except Exception:
        pass


def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        text = re.sub(r"^\s*json\s*", "", text, flags=re.I).strip()
    return text


def _loads(text: str) -> dict:
    """尽力把模型输出解析成 dict，容忍首尾多余文字。"""
    text = _strip_fence(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(text[i: j + 1])
            except json.JSONDecodeError:
                pass
        raise


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=25),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def chat_json(prompt: str, *, system: str = SYS_EXTRACTOR, model: str | None = None,
              temperature: float | None = None, max_tokens: int = 4000) -> dict:
    """调用千问并要求 JSON 输出；失败自动重试 3 次。"""
    scene = SYS_TO_SCENE.get(system, 'extractor')
    system, prompt = _apply(scene, system, prompt)
    mdl = model or CONFIG.text_model
    temp = CONFIG.temperature if temperature is None else temperature
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    kwargs = dict(model=mdl, messages=messages, temperature=temp, max_tokens=max_tokens)
    try:
        resp = text_client().chat.completions.create(response_format={"type": "json_object"}, **kwargs)
    except Exception:
        # 部分模型不支持 response_format，降级为在提示词里约束
        resp = text_client().chat.completions.create(**kwargs)
    _record(resp)
    return _loads(resp.choices[0].message.content)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=25),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def vision_json(image_bytes: bytes, prompt: str, *,
                system: str = SYS_VISION_EXTRACTOR, model: str | None = None,
                max_tokens: int = 3000) -> dict:
    """把一张图（公示截图/扫描表格）交给视觉模型，按 prompt 要求输出结构化 JSON。

    与 vision_markdown 的分工：
      vision_markdown —— 忠实全文转写（喂给后续流水线）
      vision_json     —— 直接从图里抽结构化字段（终态信息，不再进流水线）
    """
    scene = SYS_TO_SCENE.get(system, 'vision')
    system, prompt = _apply(scene, system, prompt)
    b64 = base64.b64encode(image_bytes).decode()
    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": prompt},
        ],
    }]
    if system:
        messages.insert(0, {"role": "system", "content": system})
    try:
        resp = vision_client().chat.completions.create(
            model=model or CONFIG.vision_model, messages=messages,
            temperature=0, max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
    except Exception:
        resp = vision_client().chat.completions.create(
            model=model or CONFIG.vision_model, messages=messages,
            temperature=0, max_tokens=max_tokens,
        )
    _record(resp)
    return _loads(resp.choices[0].message.content)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=25),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def vision_markdown(image_bytes: bytes, *, model: str | None = None) -> str:
    """把一页扫描件/图片交给视觉模型，转成 Markdown 文本。"""
    b64 = base64.b64encode(image_bytes).decode()
    prompt = (
        "请将这张招投标文件的扫描页完整转写为 Markdown，要求：\n"
        "1. 保留原文层级：标题用 #，正文段落照排，编号列表保持原序号；\n"
        "2. 表格必须转成 Markdown 表格（| 列1 | 列2 | 形式）；合并单元格的值在每行重复填充，不留空单元格；"
        "表格内数字逐位照抄，不得求和、排序或改写；\n"
        "3. 印章内容用〔印章：xxx〕标注，手写内容用〔手写：xxx〕标注；\n"
        "4. 看不清的字用 □ 占位，绝不猜测；\n"
        "5. 不要总结、不要省略、不要添加原文没有的内容，不要输出任何说明性文字，只输出 Markdown。"
    )
    messages = [
        {"role": "system", "content": SYS_VISION_TRANSCRIBE},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]},
    ]
    resp = vision_client().chat.completions.create(
        model=model or CONFIG.vision_model,
        messages=messages,
        temperature=0,
        max_tokens=4000,
    )
    _record(resp)
    return (resp.choices[0].message.content or "").strip()


def self_test() -> bool:
    """连通性自检。"""
    try:
        r = chat_json('请只输出JSON：{"ok": true}', max_tokens=64)
        return bool(r.get("ok"))
    except Exception as e:
        raise RuntimeError(f"模型连通性自检失败：{e}") from e
