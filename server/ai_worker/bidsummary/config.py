"""全局配置：从 .env 读取，所有可调参数集中在这里。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class ProviderSpec:
    """一个 OpenAI 兼容供应商的预设。切换模型只需在 .env 设 LLM_PROVIDER。"""
    label: str
    base_url: str
    api_key_env: str
    default_text_model: str
    default_vision_model: str | None   # None = 该供应商无视觉能力
    supports_vision: bool
    note: str = ""


# ------------------------------------------------------------------ 供应商预设
# 新增 / 调整供应商只改这里；其余代码全部走 OpenAI 兼容协议，无需改动。
PROVIDERS = {
    "dashscope": ProviderSpec(
        "阿里云百炼 / 通义千问",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
        "qwen-plus",
        "qwen3-vl-plus",
        True,
    ),
    "deepseek": ProviderSpec(
        "DeepSeek（纯文本，无视觉）",
        "https://api.deepseek.com/v1",
        "DEEPSEEK_API_KEY",
        "deepseek-chat",
        None,
        False,
        "DeepSeek 无视觉模型；公示截图识别 / 扫描页转写需另设 LLM_VISION_PROVIDER=dashscope",
    ),
    "openai": ProviderSpec(
        "OpenAI",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        "gpt-4o",
        "gpt-4o",
        True,
    ),
    "workbuddy": ProviderSpec(
        "WorkBuddy 大模型代理",
        "",  # 需显式填 LLM_BASE_URL
        "WORKBUDDY_API_KEY",
        "",  # 需显式填 LLM_TEXT_MODEL
        None,
        False,
        "需显式设置 LLM_BASE_URL / LLM_TEXT_MODEL / WORKBUDDY_API_KEY",
    ),
}


def _spec(name: str | None) -> ProviderSpec:
    name = (name or "dashscope").strip().lower()
    if name in PROVIDERS:
        return PROVIDERS[name]
    # 未知名称视为 custom：全部从显式 env 读取
    return ProviderSpec(name or "custom", "", "LLM_API_KEY", "", None, False)


def _resolve(provider_name: str | None, *, vision: bool):
    spec = _spec(provider_name)
    if vision:
        base_url = os.getenv("LLM_VISION_BASE_URL") or spec.base_url
        api_key = os.getenv("LLM_VISION_API_KEY") or os.getenv(spec.api_key_env) or ""
        model = os.getenv("LLM_VISION_MODEL") or spec.default_vision_model or ""
    else:
        # LLM_BASE_URL 优先；兼容旧 DASHSCOPE_BASE_URL；最后回落到预设 base
        base_url = os.getenv("LLM_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL") or spec.base_url
        api_key = os.getenv("LLM_API_KEY") or os.getenv(spec.api_key_env) or ""
        model = os.getenv("LLM_TEXT_MODEL") or spec.default_text_model or ""
    return base_url, api_key, model


@dataclass
class Config:
    # ---- 模型供应商（切换只需改这两个 env）----
    text_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "dashscope"))
    vision_provider: str | None = field(default_factory=lambda: os.getenv("LLM_VISION_PROVIDER"))

    # 解析后的连接参数（__post_init__ 填充）
    text_base_url: str = ""
    text_api_key: str = ""
    text_model: str = "qwen-plus"
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""
    vision_available: bool = False

    # ---- 扫描限制 ----
    max_depth: int = 5              # 目录递归深度
    max_files: int = 500            # 单任务文件数上限
    max_file_mb: int = 200          # 单文件大小上限
    archive_depth: int = 2          # 压缩包递归解压层数

    # ---- 抽取 ----
    snippet_window: int = 1200      # 关键词命中点前后各取多少字符
    max_chars_per_bidder: int = 24000   # 单个投标人喂给模型的最大字符数
    max_chars_project: int = 24000      # 项目级上下文最大字符数
    max_pages_scan: int = 40        # 单份 PDF 最多走视觉模型的页数（防烧钱）
    scan_page_dpi: int = 200        # 扫描页渲染 DPI（parsers.py 视觉识别用）
    scan_concurrency: int = 4       # 扫描页视觉识别并发数（逐页串行会把任务拖成假死）
    concurrency: int = 6            # 投标人并发数
    temperature: float = 0.0
    request_timeout: int = 180

    # ---- 置信度阈值 ----
    green: float = 0.85
    yellow: float = 0.60

    # ---- 输出 ----
    output_dir: Path = ROOT / "output"
    temp_dir: Path = ROOT / ".cache" / "extracted"

    def __post_init__(self):
        tb, tk, tm = _resolve(self.text_provider, vision=False)
        self.text_base_url, self.text_api_key, self.text_model = tb, tk, tm
        vb, vk, vm = _resolve(self.vision_provider or self.text_provider, vision=True)
        self.vision_base_url, self.vision_api_key, self.vision_model = vb, vk, vm
        self.vision_available = bool(vm)

    def model_info(self) -> dict:
        return {
            "text_provider": self.text_provider,
            "text_model": self.text_model,
            "vision_provider": self.vision_provider or self.text_provider,
            "vision_model": self.vision_model or None,
            "vision_available": self.vision_available,
        }

    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
