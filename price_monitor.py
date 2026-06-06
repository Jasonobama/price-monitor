"""
轻量级金融产品实时价格监控程序
===================================
功能：
  - 从 monitor_config.json 读取需要监控的金融产品
  - 通过新浪财经 / Yahoo Finance 实时获取价格
  - 每分钟记录一次价格到 CSV 文件
  - 支持邮件告警（从 smtp_config.json + alerts_config.json 读取配置）
   - 每周交易日结束后按分类打包 CSV 为 ZIP 归档（CSV停止录入20分钟后自动触发）
  - 每 4 周清理旧数据，提前 3 天邮件提醒
  - 收盘期间保持输出收盘价，不做特殊处理
  - 跨平台运行，Linux 下有针对性优化（daemon、cgroup、affinity 等）

用法：
  python price_monitor.py                    # 前台运行
  python price_monitor.py --daemon           # Linux 下以守护进程运行
  python price_monitor.py --config ./my.json # 指定配置文件
  python price_monitor.py --once             # 仅获取一次价格并输出

配置文件（均在程序所在目录下）：
  - monitor_config.json : 产品列表 + 全局参数 + 归档/清理设置
  - smtp_config.json    : SMTP 邮件服务器配置
  - alerts_config.json  : 价格告警规则
  - .monitor_state.json : 自动生成的调度状态文件（勿手动编辑）
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import signal
import smtplib
import sys
import threading
import time
import zipfile
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import requests


# ── 控制台编码修复（Windows 下的 UTF-8 输出） ──
def _configure_console_streams() -> None:
    """确保 stdout/stderr 使用 UTF-8 编码，解决 Windows 下中文输出乱码。"""
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── 日志文件管理（Tee 输出 + 超 500MB 自动清理） ──
class _TeeWriter:
    """同时写入原始流和日志文件的包装器。"""

    def __init__(self, original, log_handle):
        self._original = original
        self._log = log_handle

    def write(self, data):
        self._original.write(data)
        try:
            self._log.write(data)
            self._log.flush()
        except OSError:
            pass

    def flush(self):
        self._original.flush()
        try:
            self._log.flush()
        except OSError:
            pass

    def __getattr__(self, name):
        return getattr(self._original, name)


def _setup_log_file(log_path: Path) -> None:
    """打开日志文件并将 stdout/stderr 重定向为 Tee 输出。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for channel, attr in ((sys.stdout, "stdout"), (sys.stderr, "stderr")):
        try:
            fh = open(log_path, "a", encoding="utf-8")
        except OSError:
            continue
        setattr(sys, attr, _TeeWriter(channel, fh))


def _check_log_size(log_path: Path, max_bytes: Optional[int] = None) -> bool:
    """检查日志文件大小，超过阈值时截断（保留末尾 64KB 供排查）。
    返回 True 表示执行了截断。"""
    if max_bytes is None:
        max_bytes = 500 * 1024 * 1024
    if not log_path.exists():
        return False
    size = log_path.stat().st_size
    if size <= max_bytes:
        return False
    keep_bytes = 64 * 1024
    print(
        f"[日志] 日志文件 {log_path.name} 已达 {size:,} bytes，"
        f"超过 {max_bytes:,} bytes 限制，执行截断 ...",
        file=sys.stderr,
    )
    try:
        if size <= keep_bytes:
            log_path.unlink()
            print("[日志] 日志文件已删除（小于保留阈值）", file=sys.stderr)
            return True
        with open(log_path, "rb") as f:
            f.seek(-keep_bytes, os.SEEK_END)
            tail = f.read()
        log_path.write_bytes(tail)
        print(
            f"[日志] 日志文件已截断至 {log_path.stat().st_size:,} bytes",
            file=sys.stderr,
        )
        return True
    except OSError:
        try:
            log_path.unlink(missing_ok=True)
            print("[日志] 日志文件已删除", file=sys.stderr)
            return True
        except OSError:
            return False

# ═══════════════════════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════════════════════

APP_NAME = "price_monitor"
IS_LINUX = platform.system() == "Linux"

# 默认配置文件路径（与 price_monitor.py 同目录）
_BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = _BASE_DIR / "monitor_config.json"
DEFAULT_SMTP_PATH = _BASE_DIR / "smtp_config.json"
DEFAULT_ALERTS_PATH = _BASE_DIR / "alerts_config.json"
DEFAULT_CSV_DIR = _BASE_DIR / "data"
DEFAULT_ARCHIVE_DIR = _BASE_DIR / "archive"
STATE_FILE_PATH = _BASE_DIR / ".monitor_state.json"
LOG_FILE_PATH = _BASE_DIR / "monitor.log"

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
TIMEAPI_NEW_YORK_URL = "https://timeapi.io/api/TimeZone/zone?timeZone=America/New_York"
WORLD_TIME_API_NEW_YORK_URLS = (
    "https://worldtimeapi.org/api/timezone/America/New_York",
    "http://worldtimeapi.org/api/timezone/America/New_York",
)
US_DST_CACHE_SECONDS = 6 * 3600
US_DST_REQUEST_TIMEOUT = 6

# ── 预编译正则（模块级编译一次） ──
_RE_SINA_FIELD = re.compile(r'="([^"]*)"')
_RE_WHITESPACE_SPLIT = re.compile(r"\s{2,}")
_RE_SCRIPT_STYLE = re.compile(r"(?is)<(script|style).*?>.*?</\1>")
_RE_HTML_TAG = re.compile(r"(?is)<[^>]+>")
_RE_BLOCK_CLOSER = re.compile(r"(?i)</(p|div|tr|td|th|table|section|article|li|ul|ol|pre|h[1-6])>")
_RE_BR = re.compile(r"(?i)<br\s*/?>")
_RE_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_RE_SAFE_FILENAME = re.compile(r'[\\/:*?"<>|,]+')

# ── 外汇历史数据映射 ──
FX_HISTORY_TICKERS = {
    "fx_susdcnh": {"ticker": "USDCNH=X", "name": "美元/人民币", "multiplier": 1.0},
    "fx_seurcnh": {"ticker": "EURCNH=X", "name": "欧元/人民币", "multiplier": 1.0},
    "fx_sgbpcnh": {"ticker": "GBPCNH=X", "name": "英镑/人民币", "multiplier": 1.0},
    "fx_sjpycnh": {"ticker": "JPYCNH=X", "name": "百日元/人民币", "multiplier": 100.0},
}

INDEX_HISTORY_TICKERS = {
    "sh000001": {"ticker": "000001.SS", "name": "上证指数"},
    "sz399001": {"ticker": "399001.SZ", "name": "深证成指"},
    "sz399006": {"ticker": "399006.SZ", "name": "创业板指"},
}

TREASURY_HISTORY_FIELDS = {
    "2y": {"field": "BC_2YEAR", "name": "美债 2 年"},
    "5y": {"field": "BC_5YEAR", "name": "美债 5 年"},
    "10y": {"field": "BC_10YEAR", "name": "美债 10 年"},
}

# ── HTTP 会话（连接复用） ──
HTTP_SESSION = requests.Session()
HTTP_SESSION.trust_env = False

SINA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}

YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com",
    "Origin": "https://finance.yahoo.com",
}

TREASURY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://home.treasury.gov/",
}

CNBC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── 新浪财经境外VPS优化参数 ──
SINA_CONNECT_TIMEOUT = 8
SINA_READ_TIMEOUT = 15
SINA_MAX_RETRIES = 2
SINA_RETRY_BACKOFF = 1.5
SINA_OVERSEAS_LATENCY_WARN_MS = 1500

TREASURY_TEXT_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/TextView"
)

TREASURY_CACHE_SECONDS = 1800

# ── CNBC 国债收益率提取正则 ──
_RE_CNBC_YIELD_CURRENT = (
    re.compile(r"Yield\s*\|\s*[^\n%]{0,80}?([+-]?\d+\.\d+)%", re.S),
    re.compile(r"Yield\s*\|\s*.*?([+-]?\d+\.\d+)%", re.S),
)
_RE_CNBC_YIELD_PREV = (
    re.compile(r"Yield Prev Close\s*([+-]?\d+\.\d+)%"),
    re.compile(r"Prior Close \(Yield\)\s*([+-]?\d+\.\d+)%"),
    re.compile(r"Prev Close \(Yield\)\s*([+-]?\d+\.\d+)%"),
)

TREASURY_REALTIME_QUOTES = {
    "美债 2 年": {"symbol": "US2Y", "label": "U.S. 2 Year Treasury"},
    "美债 5 年": {"symbol": "US5Y", "label": "U.S. 5 Year Treasury"},
    "美债 10 年": {"symbol": "US10Y", "label": "U.S. 10 Year Treasury"},
}

# ── 全局状态 ──
_shutdown_event = threading.Event()
_csv_write_lock = threading.Lock()
_scheduler_lock = threading.Lock()  # 防止定时任务并发执行
_treasury_cache_lock = threading.Lock()
_us_dst_cache_lock = threading.Lock()

# ── 国债缓存 ──
_treasury_cache: dict = {"payload": None, "ts": 0.0}
_us_dst_cache: dict = {"payload": None, "ts": 0.0}

# ── 新浪请求统计（境外VPS优化监测量） ──
_sina_stats_lock = threading.Lock()
_sina_stats: dict = {"requests": 0, "failures": 0, "retries": 0, "total_latency_ms": 0.0}
_sina_consecutive_failures = 0


# ═══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_float(text) -> Optional[float]:
    """安全地将文本转为浮点数，失败返回 None。"""
    if text is None:
        return None
    cleaned = str(text).replace(",", "").replace("%", "").replace("+", "").strip()
    if not cleaned or cleaned in {"N/A", "None", "nan", "-"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fmt_price(value: float, decimals: int = 2, suffix: str = "") -> str:
    """格式化价格用于显示。"""
    return f"{value:,.{decimals}f}{suffix}"


def _safe_filename(name: str) -> str:
    """将名称转为安全的文件名字符串。"""
    return _RE_SAFE_FILENAME.sub("_", name).strip("_")


def _now_iso() -> str:
    """返回当前 ISO 格式日期字符串。"""
    return date.today().isoformat()


def _iso_week_str(d: date) -> str:
    """返回 ISO 周字符串，如 2026-W19。"""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _parse_api_datetime(value) -> Optional[datetime]:
    """解析 API 返回的 ISO8601 时间，并统一转为 UTC aware datetime。"""
    if not value or not isinstance(value, str):
        return None

    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> date:
    """返回某年某月第 nth 个 weekday，weekday: 0=周一 ... 6=周日。"""
    first = date(year, month, 1)
    first_match = first + timedelta(days=(weekday - first.weekday()) % 7)
    return first_match + timedelta(days=7 * (nth - 1))


def _fallback_us_dst_info(now_utc: Optional[datetime] = None) -> dict:
    """
    离线兜底：按美国现行规则计算 DST 切换点。
    起始：3 月第二个周日 02:00 EST = 07:00 UTC
    结束：11 月第一个周日 02:00 EDT = 06:00 UTC
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    year = now_utc.year
    dst_start_day = _nth_weekday_of_month(year, 3, 6, 2)
    dst_end_day = _nth_weekday_of_month(year, 11, 6, 1)
    dst_from = datetime(year, 3, dst_start_day.day, 7, tzinfo=timezone.utc)
    dst_until = datetime(year, 11, dst_end_day.day, 6, tzinfo=timezone.utc)

    return {
        "is_dst": dst_from <= now_utc < dst_until,
        "dst_from": dst_from,
        "dst_until": dst_until,
        "source": "fallback_us_rule",
        "fetched_at": now_utc,
    }


def _request_json(url: str) -> dict:
    """请求 JSON 接口并返回对象。"""
    resp = HTTP_SESSION.get(
        url,
        headers={"User-Agent": YAHOO_HEADERS["User-Agent"], "Accept": "application/json"},
        timeout=US_DST_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{url} 返回格式不是 JSON 对象")
    return payload


def _build_us_dst_info(
    is_dst: bool,
    dst_from: Optional[datetime],
    dst_until: Optional[datetime],
    source: str,
    now_utc: Optional[datetime] = None,
) -> dict:
    """统一构造 DST 信息，缺失切换点时用规则补齐。"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    fallback = _fallback_us_dst_info(now_utc)
    return {
        "is_dst": is_dst,
        "dst_from": dst_from or fallback["dst_from"],
        "dst_until": dst_until or fallback["dst_until"],
        "source": source,
        "fetched_at": datetime.now(timezone.utc),
    }


def _fetch_us_dst_info_from_timeapi() -> dict:
    """通过 TimeAPI 获取纽约时区 DST 信息。"""
    payload = _request_json(TIMEAPI_NEW_YORK_URL)
    if "isDayLightSavingActive" not in payload:
        raise ValueError("TimeAPI 返回缺少 isDayLightSavingActive")
    interval = payload.get("dstInterval") or {}
    if not isinstance(interval, dict):
        interval = {}
    return _build_us_dst_info(
        bool(payload.get("isDayLightSavingActive")),
        _parse_api_datetime(interval.get("dstStart")),
        _parse_api_datetime(interval.get("dstEnd")),
        "timeapi.io",
    )


def _fetch_us_dst_info_from_worldtimeapi() -> dict:
    """通过 WorldTimeAPI 获取纽约时区 DST 信息。"""
    last_error: Optional[Exception] = None
    for url in WORLD_TIME_API_NEW_YORK_URLS:
        try:
            payload = _request_json(url)
            if "dst" not in payload:
                raise ValueError("WorldTimeAPI 返回缺少 dst")
            now_utc = _parse_api_datetime(payload.get("utc_datetime")) or datetime.now(timezone.utc)
            return _build_us_dst_info(
                bool(payload.get("dst")),
                _parse_api_datetime(payload.get("dst_from")),
                _parse_api_datetime(payload.get("dst_until")),
                "worldtimeapi",
                now_utc,
            )
        except Exception as e:
            last_error = e
    raise last_error or RuntimeError("WorldTimeAPI 请求失败")


def _fetch_us_dst_info_from_internet() -> dict:
    """从互联网实时获取纽约时区的夏令时状态和切换点。"""
    errors = []
    for fetcher in (_fetch_us_dst_info_from_timeapi, _fetch_us_dst_info_from_worldtimeapi):
        try:
            return fetcher()
        except Exception as e:
            errors.append(f"{fetcher.__name__}: {e}")
    raise RuntimeError("; ".join(errors))


def _get_us_dst_info(force_refresh: bool = False) -> dict:
    """获取并缓存美国东部时区 DST 信息；优先联网，失败时兜底。"""
    now = time.monotonic()
    with _us_dst_cache_lock:
        cached = _us_dst_cache.get("payload")
        cached_ts = float(_us_dst_cache.get("ts") or 0.0)
        if cached and not force_refresh and now - cached_ts < US_DST_CACHE_SECONDS:
            return cached

    try:
        info = _fetch_us_dst_info_from_internet()
    except Exception as e:
        info = _fallback_us_dst_info()
        info["error"] = str(e)

    with _us_dst_cache_lock:
        _us_dst_cache["payload"] = info
        _us_dst_cache["ts"] = now

    return info


def _format_dst_info(info: dict) -> str:
    """格式化 DST 信息用于启动日志。"""
    state = "夏令时" if info.get("is_dst") else "冬令时"
    source = info.get("source", "unknown")
    dst_from = info.get("dst_from")
    dst_until = info.get("dst_until")
    if isinstance(dst_from, datetime):
        dst_from_text = dst_from.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    else:
        dst_from_text = "未知"
    if isinstance(dst_until, datetime):
        dst_until_text = dst_until.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    else:
        dst_until_text = "未知"
    extra = f"；联网失败，已使用规则兜底: {info.get('error')}" if info.get("error") else ""
    return (
        f"{state} | 来源: {source} | "
        f"开始: {dst_from_text} 北京时间 | 结束: {dst_until_text} 北京时间{extra}"
    )


def _is_us_dst() -> bool:
    """判断美国当前是否处于夏令时（优先使用联网获取的纽约时区信息）。"""
    return bool(_get_us_dst_info().get("is_dst"))


def _is_market_closed_at(now_beijing: datetime, us_is_dst: bool) -> bool:
    """
    按北京时间判断全球股市是否全部休市。
    全球最后一个收盘的市场是美国/加拿大：
      - 冬令时（EST, UTC-5）：北京时间周六 05:00 收盘 → 05:20 起休市
      - 夏令时（EDT, UTC-4）：北京时间周六 04:00 收盘 → 04:20 起休市
    周日全天休市。
    """
    now = now_beijing.astimezone(BEIJING_TZ) if now_beijing.tzinfo else now_beijing
    wd = now.weekday()
    if wd == 6:  # 周日全天休市
        return True
    if wd == 5:  # 周六：收盘+20分钟缓冲后休市
        if us_is_dst:
            # 夏令时：04:20 起休市
            return now.hour > 4 or (now.hour == 4 and now.minute >= 20)
        else:
            # 冬令时：05:20 起休市
            return now.hour > 5 or (now.hour == 5 and now.minute >= 20)
    return False  # 周一至周五正常采集


def _is_market_closed(now_beijing: Optional[datetime] = None) -> bool:
    """判断当前是否应跳过 CSV 记录。"""
    if now_beijing is None:
        now_beijing = datetime.now(BEIJING_TZ)
    return _is_market_closed_at(now_beijing, _is_us_dst())


# ═══════════════════════════════════════════════════════════════════════════════
# 状态持久化（防止定时任务重复执行）
# ═══════════════════════════════════════════════════════════════════════════════

def _load_state() -> dict:
    """加载调度状态文件。"""
    if not STATE_FILE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_FILE_PATH.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    """保存调度状态文件。"""
    try:
        STATE_FILE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[状态] 保存 .monitor_state.json 失败: {e}", file=sys.stderr)


def _state_should_run(key: str, today: str) -> bool:
    """检查某个定时任务今天是否应该执行（尚未执行过）。"""
    state = _load_state()
    last_run = state.get(key, "")
    return last_run != today


def _state_mark_run(key: str, today: str) -> None:
    """标记某个定时任务今天已执行。"""
    state = _load_state()
    state[key] = today
    _save_state(state)


# ═══════════════════════════════════════════════════════════════════════════════
# 配置加载
# ═══════════════════════════════════════════════════════════════════════════════

def load_monitor_config(config_path: Path) -> dict:
    """加载并校验主配置文件。"""
    if not config_path.exists():
        _die(f"配置文件不存在: {config_path}")

    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(f"配置文件 JSON 解析失败: {e}")

    if not isinstance(cfg, dict):
        _die("配置文件根节点必须是 JSON 对象")

    products = cfg.get("products", [])
    if not isinstance(products, list) or not products:
        _die("配置文件中 products 列表为空或格式错误")

    validated = []
    for item in products:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        source = str(item.get("source", "")).strip()
        code = str(item.get("code", "")).strip()
        category = str(item.get("category", "未分类")).strip()
        if not name or not source or not code:
            print(f"[警告] 跳过无效产品条目: {item}")
            continue
        validated.append({
            "name": name,
            "source": source,
            "code": code,
            "category": category if category else "未分类",
            "decimals": int(item.get("decimals", 2)),
            "multiplier": float(item.get("multiplier", 1.0)),
        })

    if not validated:
        _die("没有有效的产品条目，请检查配置文件")

    # ── 归档配置 ──
    archive_cfg = cfg.get("archive", {}) or {}
    archive = {
        "enabled": bool(archive_cfg.get("enabled", True)),
        "day_of_week": min(max(int(archive_cfg.get("day_of_week", 5)), 0), 6),
        "hour": min(max(int(archive_cfg.get("hour", 1)), 0), 23),
        "output_dir": str(archive_cfg.get("output_dir", "")).strip(),
    }

    # ── 清理配置 ──
    cleanup_cfg = cfg.get("cleanup", {}) or {}
    cleanup = {
        "enabled": bool(cleanup_cfg.get("enabled", True)),
        "interval_weeks": max(int(cleanup_cfg.get("interval_weeks", 4)), 1),
        "warning_days_before": max(int(cleanup_cfg.get("warning_days_before", 3)), 1),
        "keep_archives": bool(cleanup_cfg.get("keep_archives", True)),
    }

    # ── 通知配置 ──
    notify_cfg = cfg.get("notification", {}) or {}
    method = str(notify_cfg.get("method", "smtp") or "smtp").strip().lower()
    if method not in ("smtp", "telegram", "webhook", "none"):
        method = "smtp"

    tg_cfg = notify_cfg.get("telegram", {}) or {}
    wh_cfg = notify_cfg.get("webhook", {}) or {}

    notification = {
        "method": method,
        "recipient_email": str(notify_cfg.get("recipient_email", "")).strip(),
        "telegram": {
            "bot_token": str(tg_cfg.get("bot_token", "")).strip(),
            "chat_id": str(tg_cfg.get("chat_id", "")).strip(),
        },
        "webhook": {
            "url": str(wh_cfg.get("url", "")).strip(),
            "headers": dict(wh_cfg.get("headers", {}) or {}),
        },
    }

    return {
        "poll_interval_seconds": max(int(cfg.get("poll_interval_seconds", 10)), 2),
        "csv_interval_seconds": max(int(cfg.get("csv_interval_seconds", 60)), 10),
        "csv_output_dir": str(cfg.get("csv_output_dir", "")).strip(),
        "max_workers": min(max(int(cfg.get("max_workers", 4)), 1), 20),
        "products": validated,
        "archive": archive,
        "cleanup": cleanup,
        "notification": notification,
        "log_file": str(cfg.get("log_file", "")).strip(),
    }


def load_smtp_config(smtp_path: Path) -> Optional[dict]:
    """加载 SMTP 配置，不存在或无效则返回 None。"""
    if not smtp_path.exists():
        return None
    try:
        payload = json.loads(smtp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None

    host = str(payload.get("host", "")).strip()
    username = str(payload.get("username") or payload.get("email") or "").strip()
    password = str(payload.get("password") or payload.get("authorization_code") or "").strip()
    port = int(_parse_float(payload.get("port")) or 0)
    sender_name = str(payload.get("sender_name") or APP_NAME).strip() or APP_NAME
    use_tls = bool(payload.get("use_tls", False))

    if not host or not username or not password or not port:
        return None

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "sender_name": sender_name,
        "use_tls": use_tls,
    }


def load_alerts_config(alerts_path: Path) -> list[dict]:
    """加载告警配置，返回启用且有效的告警列表。"""
    if not alerts_path.exists():
        return []
    try:
        payload = json.loads(alerts_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(payload, dict):
        return []

    raw_alerts = payload.get("alerts", [])
    if not isinstance(raw_alerts, list):
        return []

    valid = []
    for entry in raw_alerts:
        if not isinstance(entry, dict):
            continue
        if not bool(entry.get("enabled", True)):
            continue

        product_name = str(entry.get("product_name", "")).strip()
        direction = str(entry.get("direction", "above")).strip().lower()
        target_price = _parse_float(entry.get("target_price"))
        recipient = str(entry.get("recipient_email", "")).strip()

        if not product_name or direction not in ("above", "below") or target_price is None or not recipient:
            continue
        if not _RE_EMAIL.fullmatch(recipient):
            continue

        valid.append({
            "product_name": product_name,
            "direction": direction,
            "target_price": target_price,
            "recipient_email": recipient,
        })

    return valid


# ═══════════════════════════════════════════════════════════════════════════════
# 数据获取 — 新浪财经
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_sina_raw(symbol: str) -> Optional[list[str]]:
    """请求新浪行情接口，返回逗号分隔的字段列表。
    境外VPS优化：分离连接/读取超时，自动重试+指数退避，统计延迟与失败。"""
    global _sina_consecutive_failures
    url = f"https://hq.sinajs.cn/rn={int(time.time() * 1000)}&list={symbol}"
    max_retries = SINA_MAX_RETRIES
    last_error: Optional[str] = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            backoff = SINA_RETRY_BACKOFF * (2 ** (attempt - 1))
            if _shutdown_event.is_set():
                return None
            _shutdown_event.wait(backoff)
            if _shutdown_event.is_set():
                return None

        req_start = time.monotonic()
        try:
            resp = HTTP_SESSION.get(
                url,
                headers=SINA_HEADERS,
                timeout=(SINA_CONNECT_TIMEOUT, SINA_READ_TIMEOUT),
            )
            elapsed = (time.monotonic() - req_start) * 1000

            resp.encoding = "gbk"
            if resp.status_code != 200 or not resp.text.strip():
                last_error = f"HTTP {resp.status_code}" if resp.status_code != 200 else "empty response"
                with _sina_stats_lock:
                    _sina_stats["failures"] += 1
                    _sina_consecutive_failures += 1
                continue

            match = _RE_SINA_FIELD.search(resp.text)
            if not match or not match.group(1).strip():
                last_error = "parse failure"
                with _sina_stats_lock:
                    _sina_stats["failures"] += 1
                    _sina_consecutive_failures += 1
                continue

            fields = match.group(1).split(",")
            with _sina_stats_lock:
                _sina_stats["requests"] += 1
                _sina_stats["total_latency_ms"] += elapsed
                _sina_consecutive_failures = 0
            if attempt > 0:
                with _sina_stats_lock:
                    _sina_stats["retries"] += 1
            return fields

        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = type(e).__name__
            with _sina_stats_lock:
                _sina_stats["failures"] += 1
                _sina_consecutive_failures += 1
        except Exception as e:
            with _sina_stats_lock:
                _sina_stats["failures"] += 1
                _sina_consecutive_failures += 1
            return None

    return None


def _get_fx_price(item: dict) -> tuple[Optional[float], Optional[float]]:
    """获取外汇价格（新浪）。返回 (price, change_pct)。"""
    fields = _fetch_sina_raw(item["code"])
    if not fields or len(fields) <= 10:
        return None, None
    try:
        price = float(fields[1])
        pct = float(fields[10].replace("%", ""))
        multiplier = item.get("multiplier", 1.0)
        return price * multiplier, pct
    except (TypeError, ValueError, IndexError):
        return None, None


def _get_index_price(item: dict) -> tuple[Optional[float], Optional[float]]:
    """获取中国股指价格（新浪）。"""
    fields = _fetch_sina_raw(item["code"])
    if not fields or len(fields) < 4:
        return None, None
    try:
        current = float(fields[3])
        previous = float(fields[2]) if fields[2] else current
        pct = (current - previous) / previous * 100 if previous else 0.0
        return current, pct
    except (TypeError, ValueError, IndexError):
        return None, None


def _get_sina_stock_price(item: dict) -> tuple[Optional[float], Optional[float]]:
    """获取A股个股价格（新浪）。"""
    fields = _fetch_sina_raw(item["code"])
    if not fields or len(fields) < 4:
        return None, None
    try:
        current = float(fields[3])
        previous = float(fields[2]) if fields[2] else current
        pct = (current - previous) / previous * 100 if previous else 0.0
        return current, pct
    except (TypeError, ValueError, IndexError):
        return None, None


# ═══════════════════════════════════════════════════════════════════════════════
# 数据获取 — Yahoo Finance
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_yahoo_quote_v7(ticker: str) -> tuple[Optional[float], Optional[float]]:
    """
    通过 Yahoo v7 quote API 获取价格（无需 crumb）。
    适用于股票、外汇(=X)、期货(=F)，比 v8 chart 更可靠。
    """
    for host in ("query1", "query2"):
        try:
            resp = HTTP_SESSION.get(
                f"https://{host}.finance.yahoo.com/v7/finance/quote",
                params={"symbols": ticker},
                headers=YAHOO_HEADERS,
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            payload = resp.json()
            results = payload.get("quoteResponse", {}).get("result") or []
            if not results:
                continue

            quote = results[0]
            current = _parse_float(quote.get("regularMarketPrice"))
            pct = _parse_float(quote.get("regularMarketChangePercent"))
            previous = _parse_float(quote.get("regularMarketPreviousClose"))
            if pct is None and current is not None and previous not in (None, 0):
                pct = (current - previous) / previous * 100
            if current is not None and pct is not None:
                return current, pct
        except Exception:
            continue
    return None, None


def _fetch_yahoo_chart_price(ticker: str) -> tuple[Optional[float], Optional[float]]:
    """
    通过 Yahoo v8 chart API 获取价格（v7 优先，v8 作为回退）。
    适用于期货、股指。
    """
    # 先尝试 v7 quote（对期货和股指更可靠）
    price, pct = _fetch_yahoo_quote_v7(ticker)
    if price is not None and pct is not None:
        return price, pct

    # 回退到 v8 chart API
    encoded = ticker.replace("^", "%5E")
    for host in ("query1", "query2"):
        url = (
            f"https://{host}.finance.yahoo.com/v8/finance/chart/"
            f"{encoded}?interval=1d&range=2d"
        )
        try:
            resp = HTTP_SESSION.get(url, headers=YAHOO_HEADERS, timeout=6)
            if resp.status_code != 200:
                continue
            payload = resp.json()
            results = payload.get("chart", {}).get("result") or []
            if not results:
                continue
            meta = results[0].get("meta") or {}
            current = _parse_float(meta.get("regularMarketPrice"))
            previous = (
                _parse_float(meta.get("chartPreviousClose"))
                or _parse_float(meta.get("previousClose"))
                or current
            )
            if current is None or previous in (None, 0):
                continue
            pct = (current - previous) / previous * 100
            return current, pct
        except Exception:
            continue
    return None, None


def _fetch_yahoo_fx_price(ticker: str, multiplier: float = 1.0) -> tuple[Optional[float], Optional[float]]:
    """
    获取 Yahoo 外汇对价格。
    v7 quote 优先 → v8 chart 回退，应用 multiplier。
    """
    price, pct = _fetch_yahoo_quote_v7(ticker)
    if price is None or pct is None:
        price, pct = _fetch_yahoo_chart_price(ticker)

    if price is not None and multiplier != 1.0:
        price = price * multiplier
    return price, pct


def _fetch_yahoo_stock_price(ticker: str, candidates: Optional[list[str]] = None) -> tuple[Optional[float], Optional[float]]:
    """
    通过 Yahoo v7 quote API 获取美股/港股价格。
    支持 candidate ticker 回退，比 chart API 更可靠，不需要 crumb。
    """
    tickers_to_try = [ticker]
    if candidates:
        tickers_to_try.extend(c for c in candidates if c != ticker)

    for tkr in tickers_to_try:
        price, pct = _fetch_yahoo_quote_v7(tkr)
        if price is not None and pct is not None:
            return price, pct

        # 回退到 chart API
        price, pct = _fetch_yahoo_chart_price(tkr)
        if price is not None and pct is not None:
            return price, pct

    return None, None


# ═══════════════════════════════════════════════════════════════════════════════
# 数据获取 — 美国国债收益率
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_cnbc_treasury_quote(symbol: str, label: str) -> tuple[Optional[float], Optional[float]]:
    """从 CNBC 获取美国国债实时收益率（通常延迟不超过15分钟）。"""
    url = f"https://www.cnbc.com/quotes/{symbol}/"
    try:
        resp = HTTP_SESSION.get(url, headers=CNBC_HEADERS, timeout=12)
        resp.raise_for_status()
    except Exception:
        return None, None

    text = resp.text
    text = _RE_SCRIPT_STYLE.sub("", text)
    text = _RE_BR.sub("\n", text)
    text = _RE_BLOCK_CLOSER.sub("\n", text)
    text = _RE_HTML_TAG.sub(" ", text)
    text = text.replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    text = "\n".join(lines)

    window = text
    index = text.find(label)
    if index != -1:
        window = text[index:index + 2400]
    else:
        window = text[:2400]

    current = None
    for compiled_pat in _RE_CNBC_YIELD_CURRENT:
        match = compiled_pat.search(window)
        if match:
            current = _parse_float(match.group(1))
            if current is not None:
                break

    previous = None
    for compiled_pat in _RE_CNBC_YIELD_PREV:
        match = compiled_pat.search(window)
        if match:
            previous = _parse_float(match.group(1))
            if previous is not None:
                break

    return current, previous


def _get_treasury_realtime_payload() -> dict:
    """通过 CNBC 获取所有期限的实时收益率。"""
    yields: dict[str, dict] = {}
    for name, config in TREASURY_REALTIME_QUOTES.items():
        current, previous = _fetch_cnbc_treasury_quote(
            str(config["symbol"]), str(config["label"])
        )
        yields[name] = {
            "value": current,
            "prev": previous,
            "source": "CNBC / Tradeweb",
        }

    return {
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "source_label": "CNBC / Tradeweb（通常延迟不超过15分钟）",
        "yields": yields,
    }


_RE_TREASURY_DATE = re.compile(r"\d{2}/\d{2}/\d{4}\b")

# 国债期限→表头匹配关键词映射，按 specificity 排序
_TREASURY_MATURITY_PATTERNS = {
    "美债 2 年": ("2 Yr", "2 Year", "2-Yr"),
    "美债 5 年": ("5 Yr", "5 Year", "5-Yr"),
    "美债 10 年": ("10 Yr", "10 Year", "10-Yr"),
}


def _find_maturity_column(header_parts: list[str], mat_patterns: tuple[str, ...]) -> int:
    """在表头行中查找期限对应的列索引，未找到返回 -1。"""
    for idx, col in enumerate(header_parts):
        col_stripped = col.strip().replace("\xa0", " ").replace("\u00a0", " ")
        for pat in mat_patterns:
            if pat.lower() in col_stripped.lower():
                return idx
    return -1


def _get_treasury_official_payload() -> dict:
    """从 U.S. Treasury 官网获取日终收益率作为回退数据源。"""
    for treasury_type in ("type=daily_treasury_yield_curve",
                          "type=daily_treasury_real_yield_curve"):
        url = f"{TREASURY_TEXT_URL}?{treasury_type}&field_tdr_date_value=2025"
        try:
            resp = HTTP_SESSION.get(url, headers=TREASURY_HEADERS, timeout=15)
            if resp.status_code != 200 or "Yield" not in resp.text:
                continue
        except Exception:
            continue

        # 提取相关行（表头 + 数据行）
        raw_lines: list[tuple[str, list[str]]] = []
        for line in resp.text.splitlines():
            if "Yield" in line or _RE_TREASURY_DATE.search(line):
                parts = [p.strip() for p in _RE_WHITESPACE_SPLIT.split(line) if p.strip()]
                if parts:
                    raw_lines.append((line, parts))

        if not raw_lines:
            continue

        # 找到表头行，确定各期限列索引
        header_parts = None
        data_rows = []
        for orig_line, parts in raw_lines:
            # 表头行: 包含 "Yield" 关键字（标题行）
            if "Yield" in orig_line and "mm" not in orig_line.lower():
                header_parts = parts
            elif header_parts is not None and _RE_TREASURY_DATE.match(parts[0]) if parts else False:
                data_rows.append(parts)

        if not header_parts or not data_rows:
            continue

        maturity_cols = {}
        for name, patterns in _TREASURY_MATURITY_PATTERNS.items():
            col = _find_maturity_column(header_parts, patterns)
            if col >= 0:
                maturity_cols[name] = col

        # 从数据行提取值（取最近两天）
        def _extract_value(col_idx: int, rows: list[list[str]]) -> tuple[Optional[float], Optional[float]]:
            latest = None
            previous = None
            for row in rows:
                if col_idx < len(row):
                    v = _parse_float(row[col_idx].replace("%", ""))
                    if v is not None:
                        if latest is None:
                            latest = v
                        elif previous is None:
                            previous = v
                            break
            return latest, previous

        yields = {}
        for name in _TREASURY_MATURITY_PATTERNS:
            col = maturity_cols.get(name, -1)
            if col >= 0:
                val, prev = _extract_value(col, data_rows)
            else:
                val, prev = None, None
            yields[name] = {"value": val, "prev": prev, "source": "U.S. Treasury"}

        return {
            "as_of": datetime.now().strftime("%Y-%m-%d"),
            "source_label": "U.S. Treasury（日终）",
            "yields": yields,
        }

    return {
        "as_of": "",
        "source_label": "获取失败",
        "yields": {},
    }


def _merge_treasury_payloads(primary: dict, fallback: dict) -> dict:
    """合并 CNBC 实时数据和 Treasury 官方数据：CNBC 优先，缺失字段用 Treasury 回退。"""
    merged = {
        "as_of": primary.get("as_of") or fallback.get("as_of") or "",
        "source_label": primary.get("source_label") or fallback.get("source_label") or "",
        "yields": {},
    }

    primary_yields = primary.get("yields", {})
    fallback_yields = fallback.get("yields", {})
    merged_yields: dict[str, dict] = {}

    for name in TREASURY_REALTIME_QUOTES:
        preferred = dict(primary_yields.get(name, {}))
        backup = dict(fallback_yields.get(name, {}))
        if preferred.get("value") is None:
            merged_yields[name] = backup
        else:
            if preferred.get("prev") is None and backup.get("prev") is not None:
                preferred["prev"] = backup.get("prev")
            merged_yields[name] = preferred

    merged["yields"] = merged_yields
    return merged


def _get_treasury_payload() -> dict:
    """
    获取美国国债收益率（CNBC 实时 + Treasury 官方双源合并）。
    线程安全，缓存 30 分钟。
    """
    now = time.time()
    with _treasury_cache_lock:
        cached = _treasury_cache.get("payload")
        cached_at = float(_treasury_cache.get("ts", 0.0))
        if cached and now - cached_at < TREASURY_CACHE_SECONDS:
            return cached  # type: ignore[return-value]

        try:
            realtime_payload = _get_treasury_realtime_payload()
            official_payload = _get_treasury_official_payload()
            payload = _merge_treasury_payloads(realtime_payload, official_payload)
        except Exception:
            if cached:
                return cached  # type: ignore[return-value]
            payload = _get_treasury_official_payload()

        _treasury_cache["ts"] = now
        _treasury_cache["payload"] = payload
        return payload


# ═══════════════════════════════════════════════════════════════════════════════
# 新浪财经境外VPS优化 — 连通性检测 & 统计报告
# ═══════════════════════════════════════════════════════════════════════════════

def _test_sina_connectivity(products: list[dict]) -> None:
    """启动时测试新浪财经连接质量，境外VPS自动检测并提示。
    使用第一个新浪源产品做3次快速采样，输出延迟与成功率。"""
    sina_products = [
        p for p in products
        if p["source"] in ("sina_fx", "sina_index", "sina_stock")
    ]
    if not sina_products:
        return

    test_prod = sina_products[0]
    latencies = []
    successes = 0

    for i in range(3):
        start = time.monotonic()
        result = _fetch_sina_raw(test_prod["code"])
        elapsed = (time.monotonic() - start) * 1000
        if result is not None:
            successes += 1
            latencies.append(elapsed)

    if successes > 0:
        avg_latency = sum(latencies) / len(latencies)
        if avg_latency > SINA_OVERSEAS_LATENCY_WARN_MS:
            print(
                f"[启动] ⚠ 新浪财经平均延迟 {avg_latency:.0f}ms "
                f"（成功率 {successes}/3，疑似境外VPS，已启用长超时+重试机制）"
            )
        else:
            print(f"[启动] 新浪财经连通性正常（平均 {avg_latency:.0f}ms，成功率 {successes}/3）")
    elif successes == 0:
        print(
            f"[启动] ⚠ 新浪财经连通性测试全部失败（3/3 超时/拒绝），"
            f"可能是境外VPS网络受限，已启用重试+指数退避机制"
        )


def _get_sina_stats_summary() -> str:
    """返回新浪请求统计摘要，用于周期性日志。"""
    with _sina_stats_lock:
        total = _sina_stats["requests"]
        fails = _sina_stats["failures"]
        retries = _sina_stats["retries"]
        total_ms = _sina_stats["total_latency_ms"]
        consecutive = _sina_consecutive_failures

    if total == 0:
        return "新浪财经: 暂无请求统计"

    avg_latency = total_ms / total if total > 0 else 0
    success_total = total + fails
    success_rate = total / success_total * 100 if success_total > 0 else 0
    return (
        f"新浪财经: 成功 {total} 次, 失败 {fails} 次 "
        f"（成功率 {success_rate:.1f}%）, "
        f"重试 {retries} 次, 平均延迟 {avg_latency:.0f}ms"
        + (f", 连续失败 {consecutive} 次 ⚠" if consecutive >= 3 else "")
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 统一价格获取入口
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_single_price(product: dict) -> dict:
    """
    获取单个产品的实时价格。
    返回: { name, price, change_pct, source_label, error }
    """
    source = product["source"]
    code = product["code"]
    name = product["name"]

    try:
        if source == "yahoo_fx":
            price, pct = _fetch_yahoo_fx_price(code, product.get("multiplier", 1.0))
            src_label = "Yahoo Finance"
        elif source == "sina_fx":
            price, pct = _get_fx_price(product)
            src_label = "新浪财经"
        elif source == "sina_index":
            price, pct = _get_index_price(product)
            src_label = "新浪财经"
        elif source == "sina_stock":
            price, pct = _get_sina_stock_price(product)
            src_label = "新浪财经"
        elif source == "yahoo_stock":
            price, pct = _fetch_yahoo_stock_price(code)
            src_label = "Yahoo Finance"
        elif source == "yahoo":
            price, pct = _fetch_yahoo_chart_price(code)
            src_label = "Yahoo Finance"
        elif source == "treasury":
            tenor = code
            field_info = TREASURY_HISTORY_FIELDS.get(tenor, {})
            label_display = field_info.get("name", name)
            payload = _get_treasury_payload()
            quote = payload.get("yields", {}).get(label_display, {})
            price = _parse_float(quote.get("value"))
            src_label = str(quote.get("source", "获取失败"))
            pct = None
        else:
            return {"name": name, "price": None, "change_pct": None,
                    "source_label": "未知数据源", "error": f"不支持的数据源: {source}"}

        if price is None:
            return {"name": name, "price": None, "change_pct": None,
                    "source_label": src_label, "error": "获取失败"}

        return {"name": name, "price": price, "change_pct": pct,
                "source_label": src_label, "error": None}

    except Exception as e:
        return {"name": name, "price": None, "change_pct": None,
                "source_label": "错误", "error": str(e)}


def fetch_all_prices(products: list[dict], max_workers: int) -> list[dict]:
    """并发获取所有产品的实时价格。"""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(fetch_single_price, p): p for p in products
        }
        for future in as_completed(future_map):
            try:
                results.append(future.result())
            except Exception as e:
                product = future_map[future]
                results.append({
                    "name": product["name"],
                    "price": None,
                    "change_pct": None,
                    "source_label": "错误",
                    "error": str(e),
                })
    order = {p["name"]: i for i, p in enumerate(products)}
    results.sort(key=lambda r: order.get(r["name"], 999))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# CSV 记录
# ═══════════════════════════════════════════════════════════════════════════════

def _get_csv_path(csv_dir: Path, timestamp: datetime) -> Path:
    """根据时间戳生成每日 CSV 文件路径。"""
    date_str = timestamp.strftime("%Y-%m-%d")
    return csv_dir / f"prices_{date_str}.csv"


def _build_csv_header(products: list[dict]) -> list[str]:
    """构造 CSV 表头。"""
    header = ["timestamp"]
    for p in products:
        safe_name = _safe_filename(p["name"])
        header.append(f"{safe_name}_price")
        header.append(f"{safe_name}_change_pct")
    return header


def write_csv_row(
    csv_dir: Path,
    products: list[dict],
    prices: list[dict],
    timestamp: datetime,
) -> None:
    """
    将当前价格快照写入当日的 CSV 文件（线程安全）。
    每行格式: timestamp, prod1_price, prod1_pct, prod2_price, prod2_pct, ...
    """
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = _get_csv_path(csv_dir, timestamp)
    header = _build_csv_header(products)

    price_map = {r["name"]: r for r in prices}

    row = [timestamp.strftime("%Y-%m-%d %H:%M:%S")]
    for p in products:
        info = price_map.get(p["name"], {})
        row.append(f"{info.get('price', '')}" if info.get("price") is not None else "")
        row.append(f"{info.get('change_pct', '')}" if info.get("change_pct") is not None else "")

    with _csv_write_lock:
        file_exists = csv_path.exists()
        try:
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(header)
                writer.writerow(row)
        except OSError as e:
            print(f"[CSV错误] 写入 {csv_path} 失败: {e}", file=sys.stderr)


def _collect_weekly_csv_files(csv_dir: Path, week_start: date, week_end: date) -> list[Path]:
    """收集指定日期范围内的每日 CSV 文件（按日期排序）。"""
    files = []
    if not csv_dir.exists():
        return files
    current = week_start
    while current <= week_end:
        csv_path = _get_csv_path(csv_dir, datetime(current.year, current.month, current.day))
        if csv_path.exists():
            files.append(csv_path)
        current += timedelta(days=1)
    return sorted(files)


def _category_columns_map(products: list[dict]) -> dict[str, list[int]]:
    """
    返回 {category: [col_index, ...]} 映射。
    col_index 对应 CSV 中的列位置（从 0 开始，跳过 timestamp 列）。
    """
    mapping: dict[str, list[int]] = {}
    col = 1  # 跳过 timestamp
    for p in products:
        cat = p.get("category", "未分类")
        mapping.setdefault(cat, []).extend([col, col + 1])
        col += 2
    return mapping


# ═══════════════════════════════════════════════════════════════════════════════
# 邮件告警
# ═══════════════════════════════════════════════════════════════════════════════

def _check_alert_condition(direction: str, price: float, target: float) -> bool:
    """判断当前价格是否触发告警条件。"""
    if direction == "below":
        return price <= target
    return price >= target


def _send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    """通过 Telegram Bot API 发送通知（无需存储任何密码）。"""
    import html as _html
    safe_text = _html.escape(text, quote=False)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": safe_text, "parse_mode": "HTML"}
    resp = HTTP_SESSION.post(url, json=payload, timeout=12)
    result = resp.json()
    if not result.get("ok"):
        desc = result.get("description", "unknown")
        if "chat not found" in str(desc).lower():
            raise RuntimeError(
                "Telegram 发送失败：请先在 Telegram 中向你的 Bot 发送一条消息（如 /start），"
                "然后重试。详情: " + str(desc)
            )
        raise RuntimeError(f"Telegram API 错误: {desc}")
    resp.raise_for_status()


def _send_telegram_document(bot_token: str, chat_id: str, file_path: Path, caption: str = "") -> None:
    """通过 Telegram Bot API 的 sendDocument 接口发送文件。"""
    file_size = file_path.stat().st_size
    max_size = 50 * 1024 * 1024
    if file_size > max_size:
        raise ValueError(
            f"文件 {file_path.name} ({file_size:,} bytes) 超过 Telegram 50MB 限制，跳过发送"
        )

    import html as _html
    safe_caption = _html.escape(caption, quote=False)
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"

    with open(file_path, "rb") as fh:
        resp = HTTP_SESSION.post(
            url,
            data={"chat_id": chat_id, "caption": safe_caption, "parse_mode": "HTML"},
            files={"document": (file_path.name, fh, "application/zip")},
            timeout=60,
        )
    result = resp.json()
    if not result.get("ok"):
        desc = result.get("description", "unknown")
        raise RuntimeError(f"Telegram sendDocument 失败: {desc}")
    resp.raise_for_status()


def _test_telegram_communication(notify_cfg: dict) -> None:
    """单次测试 Telegram Bot 连通性，确认 VPS 与 Telegram 机器人能正常通讯。"""
    method = notify_cfg.get("method", "smtp")
    if method != "telegram":
        print(f"[Telegram测试] 当前通知方式为 \"{method}\"，非 Telegram 模式，跳过测试")
        print(f"[Telegram测试] 如需测试 Telegram，请先在 monitor_config.json 中设置 notification.method = \"telegram\"")
        return

    tg = notify_cfg.get("telegram", {}) or {}
    bot_token = str(tg.get("bot_token", "")).strip()
    chat_id = str(tg.get("chat_id", "")).strip()

    if not bot_token:
        _die("Telegram bot_token 未配置")
    if not chat_id:
        _die("Telegram chat_id 未配置")

    print(f"[Telegram测试] bot_token: {bot_token[:12]}...{bot_token[-4:]}")
    print(f"[Telegram测试] chat_id: {chat_id}")
    print(f"[Telegram测试] 正在验证 Bot Token ...")

    import html as _html
    try:
        resp = HTTP_SESSION.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=10)
        result = resp.json()
        if not result.get("ok"):
            print(f"[Telegram测试] ✗ Bot Token 无效: {result.get('description', '未知错误')}")
            return
        bot_info = result.get("result", {})
        print(f"[Telegram测试] ✓ Bot Token 有效 (@{bot_info.get('username', '未知')})")
    except requests.ConnectionError:
        print(f"[Telegram测试] ✗ 无法连接 api.telegram.org（网络不通）")
        return
    except Exception as e:
        print(f"[Telegram测试] ✗ getMe 失败: {e}")
        return

    test_text = _html.escape(
        "✅ Telegram 通讯测试成功！\n\n"
        f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"程序: {APP_NAME}",
        quote=False,
    )
    print(f"[Telegram测试] 正在发送测试消息 ...")
    try:
        resp = HTTP_SESSION.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": test_text, "parse_mode": "HTML"},
            timeout=12,
        )
        result = resp.json()
        if result.get("ok"):
            print(f"[Telegram测试] ✓ 测试消息发送成功！")
        else:
            desc = result.get("description", "未知错误")
            print(f"[Telegram测试] ✗ 发送失败: {desc}")
    except Exception as e:
        print(f"[Telegram测试] ✗ 发送异常: {e}")
    print()


def _test_telegram_file_send(notify_cfg: dict, file_path: Path) -> None:
    """单次测试 Telegram 文件发送功能。"""
    method = notify_cfg.get("method", "smtp")
    if method != "telegram":
        print(f"[Telegram文件测试] 当前通知方式为 \"{method}\"，非 Telegram 模式，跳过")
        return

    if not file_path.exists():
        _die(f"文件不存在: {file_path}")

    tg = notify_cfg.get("telegram", {}) or {}
    bot_token = str(tg.get("bot_token", "")).strip()
    chat_id = str(tg.get("chat_id", "")).strip()
    if not bot_token or not chat_id:
        _die("Telegram bot_token 或 chat_id 未配置")

    fsize = file_path.stat().st_size
    print(f"[Telegram文件测试] 文件: {file_path.name} ({fsize:,} bytes)")
    print(f"[Telegram文件测试] 正在发送到 chat_id={chat_id} ...")

    try:
        _send_telegram_document(bot_token, chat_id, file_path, caption=f"测试文件: {file_path.name}\n大小: {fsize:,} bytes")
        print(f"[Telegram文件测试] ✓ 文件发送成功！请检查 Telegram 中是否收到文件。")
    except Exception as e:
        print(f"[Telegram文件测试] ✗ 文件发送失败: {e}")
    print()


def _send_webhook(url: str, text: str, headers: Optional[dict] = None) -> None:
    """通过通用 Webhook 发送通知（支持 Discord/Slack/企业微信/自定义）。"""
    if headers is None:
        headers = {}
    payload = {"content": text}
    resp = HTTP_SESSION.post(url, json=payload, headers=headers, timeout=12)
    resp.raise_for_status()


def _notify(
    notify_cfg: dict,
    subject: str,
    body: str,
    smtp_cfg: Optional[dict] = None,
    attachments: Optional[list[Path]] = None,
) -> None:
    """
    统一通知调度：根据 method 选择 SMTP / Telegram / Webhook。
    - smtp: 使用 smtp_cfg（从 smtp_config.json 加载），附件通过 EmailMessage 发送
    - telegram: 使用 notify_cfg.telegram.bot_token + chat_id，附件通过 sendDocument 逐文件发送
    - webhook: 使用 notify_cfg.webhook.url + headers
    - none: 静默跳过
    attachments: 可选的文件路径列表，仅 SMTP 和 Telegram 支持
    """
    method = notify_cfg.get("method", "smtp")
    text = subject + "\n\n" + body

    if method == "telegram":
        tg = notify_cfg.get("telegram", {})
        bot_token = str(tg.get("bot_token", "")).strip()
        chat_id = str(tg.get("chat_id", "")).strip()
        if not bot_token or not chat_id:
            raise ValueError("Telegram 通知未配置 bot_token 或 chat_id")
        _send_telegram(bot_token, chat_id, text)
        if attachments:
            for fp in attachments:
                if not fp.exists():
                    print(f"  [通知] 跳过不存在的附件: {fp.name}", file=sys.stderr)
                    continue
                try:
                    _send_telegram_document(bot_token, chat_id, fp)
                    print(f"  [通知] Telegram 已发送文件: {fp.name}")
                except Exception as e:
                    print(f"  [通知] Telegram 发送文件 {fp.name} 失败: {e}", file=sys.stderr)

    elif method == "webhook":
        wh = notify_cfg.get("webhook", {})
        url = str(wh.get("url", "")).strip()
        headers = wh.get("headers", {}) or {}
        if not url:
            raise ValueError("Webhook 通知未配置 url")
        _send_webhook(url, text, headers)
        if attachments:
            print(f"  [通知] Webhook 模式不支持附件发送，已跳过 {len(attachments)} 个文件")

    elif method == "smtp":
        if not smtp_cfg:
            raise ValueError("SMTP 通知需要 smtp_config.json")
        recipient = notify_cfg.get("recipient_email", "")
        if not recipient:
            raise ValueError("SMTP 通知未配置 recipient_email")
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{smtp_cfg['sender_name']} <{smtp_cfg['username']}>"
        message["To"] = recipient
        message.set_content(body, subtype="plain", charset="utf-8")
        if attachments:
            for fp in attachments:
                if not fp.exists():
                    continue
                mime_type = "application/zip" if fp.suffix.lower() == ".zip" else "application/octet-stream"
                maintype, subtype = mime_type.split("/", 1)
                message.add_attachment(
                    fp.read_bytes(),
                    maintype=maintype,
                    subtype=subtype,
                    filename=fp.name,
                )
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=20) as smtp:
            smtp.ehlo()
            if smtp_cfg["use_tls"]:
                smtp.starttls()
                smtp.ehlo()
            smtp.login(smtp_cfg["username"], smtp_cfg["password"])
            smtp.send_message(message)

    elif method == "none":
        pass
    else:
        raise ValueError(f"不支持的通知方式: {method}")


def send_alert_notification(
    notify_cfg: dict,
    smtp_cfg: Optional[dict],
    alert: dict,
    current_price: float,
    product: dict,
) -> None:
    """发送价格告警通知（SMTP / Telegram / Webhook）。"""
    direction_text = "高于或等于" if alert["direction"] == "above" else "低于或等于"
    decimals = product.get("decimals", 2)
    target_str = _fmt_price(alert["target_price"], decimals)
    price_str = _fmt_price(current_price, decimals)
    product_name = alert["product_name"]

    subject = f"[价格告警] {product_name} {direction_text} {target_str}"
    body_lines = [
        "价格告警已触发。",
        "",
        f"产品: {product_name}",
        f"触发条件: {direction_text} {target_str}",
        f"当前价格: {price_str}",
        f"触发时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"该通知由 {APP_NAME} 自动发送。",
    ]
    _notify(notify_cfg, subject, "\n".join(body_lines), smtp_cfg)


def check_and_send_alerts(
    notify_cfg: dict,
    smtp_cfg: Optional[dict],
    alerts: list[dict],
    products: list[dict],
    current_prices: list[dict],
    alert_states: dict[str, bool],
) -> None:
    """
    检查所有告警条件，触发时发送通知。
    使用边缘触发（仅当条件从不满足变为满足时才发送）。
    """
    price_map = {r["name"]: r for r in current_prices}
    product_map = {p["name"]: p for p in products}

    for alert in alerts:
        product_name = alert["product_name"]
        info = price_map.get(product_name)
        if info is None or info["price"] is None:
            continue

        current_price = info["price"]
        direction = alert["direction"]
        target = alert["target_price"]
        alert_id = f"{product_name}:{direction}:{target}"

        condition_met = _check_alert_condition(direction, current_price, target)
        previously_met = alert_states.get(alert_id, False)

        alert_states[alert_id] = condition_met

        if condition_met and not previously_met:
            product = product_map.get(product_name, {"decimals": 2})
            try:
                send_alert_notification(notify_cfg, smtp_cfg, alert, current_price, product)
                print(
                    f"[告警] {product_name} {direction} {_fmt_price(target, product.get('decimals', 2))}"
                    f" 已触发！当前价格: {_fmt_price(current_price, product.get('decimals', 2))}"
                )
            except Exception as e:
                print(f"[告警错误] 发送通知失败: {e}", file=sys.stderr)
        elif not condition_met:
            alert_states[alert_id] = False


# ═══════════════════════════════════════════════════════════════════════════════
# 每周归档（分类压缩）
# ═══════════════════════════════════════════════════════════════════════════════

class WeeklyArchiver:
    """每周交易日结束后，将 CSV 按分类打包成 ZIP 归档。"""

    def __init__(
        self,
        archive_cfg: dict,
        csv_dir: Path,
        products: list[dict],
    ):
        self.enabled = archive_cfg["enabled"]
        self.day_of_week = archive_cfg["day_of_week"]
        self.hour = archive_cfg["hour"]
        self.csv_dir = csv_dir

        out_dir = archive_cfg.get("output_dir", "")
        self.archive_dir = Path(out_dir) if out_dir else DEFAULT_ARCHIVE_DIR

        # 预计算产品分类→列映射
        self.cat_columns = _category_columns_map(products)
        # 产品名列表（保持顺序）
        self.product_names = [p["name"] for p in products]
        self.products = products

    def _get_week_boundaries(self, today: date) -> tuple[date, date]:
        """计算本周的起始（周一）和结束日期。"""
        weekday = today.weekday()  # 0=周一
        week_start = today - timedelta(days=weekday)
        # 结束日期为周六（包括周日可能的数据）
        week_end = week_start + timedelta(days=5)  # 周六
        return week_start, week_end

    def check_and_run(
        self,
        notify_cfg: dict,
        smtp_cfg: Optional[dict],
    ) -> None:
        """
        当主循环判定"CSV 停止录入后已过 20 分钟"时调用此方法执行归档。
        不再依赖 VPS 系统时间（day_of_week / hour），仅做去重和线程安全控制。
        """
        if not self.enabled:
            return

        now = datetime.now()
        today = now.date()
        today_str = today.isoformat()

        if not _state_should_run("weekly_archive", today_str):
            return

        acquired = _scheduler_lock.acquire(blocking=False)
        if not acquired:
            return

        try:
            week_start, week_end = self._get_week_boundaries(today)
            week_str = _iso_week_str(week_start)
            csv_files = _collect_weekly_csv_files(self.csv_dir, week_start, week_end)

            if not csv_files:
                print(f"[归档] 第 {week_str} 周无 CSV 数据，跳过归档")
                _state_mark_run("weekly_archive", today_str)
                return

            # 分离线：生成分类 CSV 并打包
            cat_files: dict[str, list[Path]] = {}  # {category: [temp_csv_path]}
            temp_files: list[Path] = []

            for cat_name, col_indices in self.cat_columns.items():
                safe_cat = _safe_filename(cat_name)
                temp_csv = self.archive_dir / f"_tmp_{week_str}_{safe_cat}.csv"
                temp_files.append(temp_csv)

                rows_written = self._build_category_csv(
                    csv_files, col_indices, temp_csv, cat_name
                )
                if rows_written > 0:
                    cat_files[cat_name] = [temp_csv]

            # ── 打包为 ZIP ──
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            created_zips = []

            for cat_name, file_list in cat_files.items():
                safe_cat = _safe_filename(cat_name)
                zip_path = self.archive_dir / f"{safe_cat}_{week_str}.zip"
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fp in file_list:
                        # 在 ZIP 内使用简洁的文件名
                        arcname = f"{safe_cat}_{week_str}.csv"
                        zf.write(fp, arcname=arcname)
                created_zips.append((cat_name, zip_path))

            # ── 清理临时文件 ──
            for tf in temp_files:
                with suppress(OSError):
                    tf.unlink()

            if created_zips:
                zip_list = "\n".join(
                    f"    {cat}: {zp.name} ({zp.stat().st_size:,} bytes)"
                    for cat, zp in created_zips
                )
                print(f"[归档] 第 {week_str} 周归档完成:\n{zip_list}")

                # ── 发送归档通知 ──
                try:
                    self._send_archive_notification(
                        notify_cfg, smtp_cfg, week_str, created_zips
                    )
                    print(f"[归档] 已发送归档通知")
                except Exception as e:
                    print(f"[归档错误] 发送通知失败: {e}", file=sys.stderr)
            else:
                print(f"[归档] 第 {week_str} 周无可归档的分类数据")

            _state_mark_run("weekly_archive", today_str)

        except Exception as e:
            print(f"[归档错误] 执行失败: {e}", file=sys.stderr)
        finally:
            _scheduler_lock.release()

    def force_run(
        self,
        notify_cfg: dict,
        smtp_cfg: Optional[dict],
        reference_date: Optional[date] = None,
    ) -> bool:
        """手动触发归档，绕过日期/时间检查。可用于 --archive-now 测试。
        reference_date: 以该日为基准计算归档周范围，默认今天。
        返回 True 表示生成了 ZIP 文件。"""
        today = reference_date or date.today()
        today_str = today.isoformat()

        print(f"[归档-手动] 正在对 {today_str} 所在周执行归档 ...")

        acquired = _scheduler_lock.acquire(blocking=False)
        if not acquired:
            print(f"[归档-手动] 已有归档任务正在执行，请稍后重试")
            return False

        try:
            week_start, week_end = self._get_week_boundaries(today)
            week_str = _iso_week_str(week_start)
            csv_files = _collect_weekly_csv_files(self.csv_dir, week_start, week_end)

            if not csv_files:
                print(
                    f"[归档-手动] 第 {week_str} 周（{week_start} ~ {week_end}）无 CSV 数据，跳过归档"
                )
                return False

            cat_files: dict[str, list[Path]] = {}
            temp_files: list[Path] = []

            for cat_name, col_indices in self.cat_columns.items():
                safe_cat = _safe_filename(cat_name)
                temp_csv = self.archive_dir / f"_tmp_{week_str}_{safe_cat}.csv"
                temp_files.append(temp_csv)

                rows_written = self._build_category_csv(
                    csv_files, col_indices, temp_csv, cat_name
                )
                if rows_written > 0:
                    cat_files[cat_name] = [temp_csv]

            self.archive_dir.mkdir(parents=True, exist_ok=True)
            created_zips = []

            for cat_name, file_list in cat_files.items():
                safe_cat = _safe_filename(cat_name)
                zip_path = self.archive_dir / f"{safe_cat}_{week_str}.zip"
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fp in file_list:
                        arcname = f"{safe_cat}_{week_str}.csv"
                        zf.write(fp, arcname=arcname)
                created_zips.append((cat_name, zip_path))

            for tf in temp_files:
                with suppress(OSError):
                    tf.unlink()

            if created_zips:
                zip_list = "\n".join(
                    f"    {cat}: {zp.name} ({zp.stat().st_size:,} bytes)  →  {zp}"
                    for cat, zp in created_zips
                )
                print(f"[归档-手动] 第 {week_str} 周归档完成:\n{zip_list}")

                try:
                    self._send_archive_notification(
                        notify_cfg, smtp_cfg, week_str, created_zips
                    )
                    print(f"[归档-手动] 已发送归档通知")
                except Exception as e:
                    print(f"[归档-手动] 发送通知失败: {e}", file=sys.stderr)

                return True
            else:
                print(f"[归档-手动] 第 {week_str} 周无可归档的分类数据")
                return False

        except Exception as e:
            print(f"[归档-手动] 执行失败: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            return False
        finally:
            _scheduler_lock.release()

    def _build_category_csv(
        self,
        source_files: list[Path],
        col_indices: list[int],
        output_path: Path,
        cat_name: str,
    ) -> int:
        """
        从每日 CSV 中提取指定分类的列，生成单一分类 CSV。
        返回写入的行数。
        """
        # 收集该分类的产品名
        cat_products = [p for p in self.products if p.get("category") == cat_name]
        if not cat_products:
            return 0

        rows_written = 0
        with open(output_path, "w", newline="", encoding="utf-8") as out_f:
            writer = csv.writer(out_f)

            for src_path in source_files:
                try:
                    with open(src_path, "r", encoding="utf-8") as in_f:
                        reader = csv.reader(in_f)
                        header = next(reader, None)
                        if header is None:
                            continue

                        # 第一次写表头
                        if rows_written == 0:
                            out_header = ["timestamp"]
                            for p in cat_products:
                                safe = _safe_filename(p["name"])
                                out_header.append(f"{safe}_price")
                                out_header.append(f"{safe}_change_pct")
                            writer.writerow(out_header)

                        # 遍历数据行
                        for row in reader:
                            if len(row) < max(col_indices) + 1 if col_indices else 2:
                                continue
                            new_row = [row[0]]  # timestamp
                            for ci in col_indices:
                                new_row.append(row[ci] if ci < len(row) else "")
                            writer.writerow(new_row)
                            rows_written += 1
                except Exception as e:
                    print(f"[归档] 读取 {src_path.name} 失败: {e}", file=sys.stderr)
                    continue

        return rows_written

    def _send_archive_notification(
        self,
        notify_cfg: dict,
        smtp_cfg: Optional[dict],
        week_str: str,
        zips: list[tuple[str, Path]],
    ) -> None:
        """发送每周归档完成通知，并将 ZIP 文件作为附件发送给客户。"""
        zip_list = "\n".join(
            f"  [{cat}] {zp.name} ({zp.stat().st_size:,} bytes)"
            for cat, zp in zips
        )
        archive_dir_str = str(self.archive_dir.resolve())

        subject = f"[归档通知] 第 {week_str} 周数据已归档"
        body = "\n".join([
            f"第 {week_str} 周金融产品价格数据已按分类打包完成。",
            "",
            "归档文件:",
            zip_list,
            "",
            f"存储位置: {archive_dir_str}",
            "",
            "数据来源: 新浪财经 / Yahoo Finance / U.S. Treasury",
            f"归档时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"该通知由 {APP_NAME} 自动发送。",
        ])
        attachment_paths = [zp for _, zp in zips if zp.exists()]
        _notify(notify_cfg, subject, body, smtp_cfg, attachments=attachment_paths)


# ═══════════════════════════════════════════════════════════════════════════════
# 数据清理（每 4 周清理 + 提前 3 天提醒）
# ═══════════════════════════════════════════════════════════════════════════════

class DataCleaner:
    """定期清理旧 CSV 数据，清理前发送提醒邮件。"""

    def __init__(self, cleanup_cfg: dict, csv_dir: Path):
        self.enabled = cleanup_cfg["enabled"]
        self.interval_weeks = cleanup_cfg["interval_weeks"]
        self.warning_days_before = cleanup_cfg["warning_days_before"]
        self.keep_archives = cleanup_cfg["keep_archives"]
        self.csv_dir = csv_dir

    def check_and_run(
        self,
        notify_cfg: dict,
        smtp_cfg: Optional[dict],
    ) -> None:
        """
        检查是否需要发送清理提醒或执行清理。
        线程安全（通过 _scheduler_lock）。
        """
        if not self.enabled:
            return

        now = datetime.now()
        today = now.date()
        today_str = today.isoformat()

        # ── 计算下次清理日期 ──
        # 从 .monitor_state.json 中读取上次清理日期
        state = _load_state()
        last_cleanup_str = state.get("last_cleanup", "")

        if last_cleanup_str:
            try:
                last_cleanup = date.fromisoformat(last_cleanup_str)
            except ValueError:
                last_cleanup = today - timedelta(weeks=self.interval_weeks)
        else:
            # 从未清理过：假设从程序启动那天开始计时
            last_cleanup = today - timedelta(weeks=self.interval_weeks)

        next_cleanup = last_cleanup + timedelta(weeks=self.interval_weeks)
        warning_date = next_cleanup - timedelta(days=self.warning_days_before)

        # ── 检查是否需要发送提醒（在 warning_date 当天或之后，且清理尚未发生） ──
        if today >= warning_date and today < next_cleanup:
            if _state_should_run("cleanup_warning", today_str):
                self._send_cleanup_warning(notify_cfg, smtp_cfg, next_cleanup)
                _state_mark_run("cleanup_warning", today_str)
                return

        # ── 检查是否需要执行清理 ──
        if today < next_cleanup:
            return

        if not _state_should_run("last_cleanup", today_str):
            return

        acquired = _scheduler_lock.acquire(blocking=False)
        if not acquired:
            return

        try:
            deleted_count = self._perform_cleanup()
            print(f"[清理] 已删除 {deleted_count} 个过期 CSV 文件")

            # ── 发送清理完成通知 ──
            try:
                self._send_cleanup_done_notification(
                    notify_cfg, smtp_cfg, deleted_count
                )
                print(f"[清理] 已发送清理完成通知")
            except Exception as e:
                print(f"[清理错误] 发送通知失败: {e}", file=sys.stderr)

            _state_mark_run("last_cleanup", today_str)
            # 同时更新清理提醒状态，防止重复
            _state_mark_run("cleanup_warning", today_str)

        except Exception as e:
            print(f"[清理错误] 执行失败: {e}", file=sys.stderr)
        finally:
            _scheduler_lock.release()

    def _perform_cleanup(self) -> int:
        """
        删除超过 interval_weeks 周的 CSV 文件。
        返回删除的文件数。
        """
        if not self.csv_dir.exists():
            return 0

        cutoff = date.today() - timedelta(weeks=self.interval_weeks)
        deleted = 0

        for entry in self.csv_dir.iterdir():
            if not entry.is_file():
                continue
            if not entry.name.startswith("prices_") or not entry.suffix == ".csv":
                continue
            try:
                # prices_2026-04-13.csv → date(2026, 4, 13)
                date_part = entry.stem.replace("prices_", "")
                file_date = date.fromisoformat(date_part)
            except (ValueError, IndexError):
                continue

            if file_date < cutoff:
                try:
                    entry.unlink()
                    deleted += 1
                except OSError as e:
                    print(f"[清理] 删除 {entry.name} 失败: {e}", file=sys.stderr)

        return deleted

    def _send_cleanup_warning(
        self,
        notify_cfg: dict,
        smtp_cfg: Optional[dict],
        cleanup_date: date,
    ) -> None:
        """发送清理前提醒通知。"""
        days_left = (cleanup_date - date.today()).days
        subject = f"[清理提醒] {days_left} 天后将清理 {self.interval_weeks} 周前的旧数据"

        body = "\n".join([
            f"数据清理提醒",
            "",
            f"您的金融产品价格监控数据将按计划清理。",
            "",
            f"清理日期: {cleanup_date.strftime('%Y-%m-%d')}（{days_left} 天后）",
            f"清理范围: {self.interval_weeks} 周前的 CSV 日数据",
            f"归档保留: {'是' if self.keep_archives else '否'}（归档 ZIP 文件将{'保留' if self.keep_archives else '被删除'}）",
            "",
            f"CSV 数据目录: {self.csv_dir.resolve()}",
            "",
            "如需保存历史数据，请在此之前自行备份。",
            "您也可以编辑 monitor_config.json 调整清理设置。",
            "",
            f"该通知由 {APP_NAME} 自动发送。",
        ])
        _notify(notify_cfg, subject, body, smtp_cfg)
        print(f"[清理] 已发送清理提醒通知（清理日期: {cleanup_date}）")

    def _send_cleanup_done_notification(
        self,
        notify_cfg: dict,
        smtp_cfg: Optional[dict],
        deleted_count: int,
    ) -> None:
        """发送清理完成通知。"""
        subject = f"[清理完成] 已删除 {deleted_count} 个过期 CSV 文件"
        body = "\n".join([
            "数据清理已完成。",
            "",
            f"清理日期: {date.today().isoformat()}",
            f"删除文件: {deleted_count} 个（{self.interval_weeks} 周前的 CSV 日数据）",
            f"归档保留: {'是' if self.keep_archives else '否'}",
            "",
            f"CSV 数据目录: {self.csv_dir.resolve()}",
            "",
            f"该通知由 {APP_NAME} 自动发送。",
        ])
        _notify(notify_cfg, subject, body, smtp_cfg)


# ═══════════════════════════════════════════════════════════════════════════════
# 终端输出
# ═══════════════════════════════════════════════════════════════════════════════

def _format_output_row(info: dict, decimals: int) -> str:
    """格式化单行输出。"""
    name = info["name"]
    if info["error"]:
        return f"  {name:<20s} 获取失败 ({info['error'][:40]})"

    price = info["price"]
    pct = info["change_pct"]
    price_str = _fmt_price(price, decimals)
    if pct is not None:
        sign = "+" if pct >= 0 else ""
        pct_str = f"{sign}{pct:.2f}%"
    else:
        pct_str = "N/A"
    src = info.get("source_label", "")

    return f"  {name:<20s} {price_str:>12s}  {pct_str:>10s}  [{src}]"


def _get_console_width() -> int:
    """获取终端宽度。"""
    try:
        return os.get_terminal_size().columns
    except (OSError, ValueError):
        return 80


def print_price_snapshot(
    prices: list[dict],
    products: list[dict],
    iteration: int,
    clear_screen: bool = False,
) -> None:
    """打印当前价格快照。"""
    width = _get_console_width()
    decimals_map = {p["name"]: p.get("decimals", 2) for p in products}

    if clear_screen:
        os.system("cls" if os.name == "nt" else "clear")

    print("=" * min(width, 80))
    print(f"  金融产品实时价格监控  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  第 {iteration} 次")
    print("-" * min(width, 80))

    success_count = 0
    fail_count = 0
    for info in prices:
        dec = decimals_map.get(info["name"], 2)
        print(_format_output_row(info, dec))
        if info["price"] is not None:
            success_count += 1
        else:
            fail_count += 1

    print("-" * min(width, 80))
    print(f"  成功: {success_count}/{len(prices)}  |  失败: {fail_count}/{len(prices)}")
    print("=" * min(width, 80))
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════════════════════

def run_monitor_loop(
    config: dict,
    smtp_cfg: Optional[dict],
    alerts: list[dict],
    csv_dir: Path,
    once: bool = False,
    log_path: Path = LOG_FILE_PATH,
) -> None:
    """主监控循环。"""
    products = config["products"]
    poll_interval = config["poll_interval_seconds"]
    csv_interval = config["csv_interval_seconds"]
    max_workers = config["max_workers"]
    notify_cfg = config["notification"]
    notification_email = notify_cfg.get("recipient_email", "")

    alert_states: dict[str, bool] = {}

    # ── 初始化定时任务 ──
    archiver = WeeklyArchiver(config["archive"], csv_dir, products)
    cleaner = DataCleaner(config["cleanup"], csv_dir)
    dst_info = _get_us_dst_info()

    print(f"[启动] 监控 {len(products)} 个金融产品")
    print(f"[启动] 轮询间隔: {poll_interval}s  |  CSV 记录间隔: {csv_interval}s")
    print(f"[启动] CSV 输出目录: {csv_dir}")
    print(f"[启动] 并发线程数: {max_workers}")
    print(f"[启动] 美国冬夏令时: {_format_dst_info(dst_info)}")
    method = notify_cfg.get("method", "smtp")
    notify_desc = {"smtp": "SMTP邮件", "telegram": "Telegram", "webhook": "Webhook", "none": "已禁用"}
    print(f"[启动] 通知方式: {notify_desc.get(method, method)}")
    if config["archive"]["enabled"]:
        print(f"[启动] 每周归档: 已启用（CSV 停止录入后 20 分钟自动触发，不依赖系统时间）")
    if config["cleanup"]["enabled"]:
        print(f"[启动] 数据清理: 已启用（每 {config['cleanup']['interval_weeks']} 周，"
              f"提前 {config['cleanup']['warning_days_before']} 天提醒）")
    if alerts:
        print(f"[启动] 价格告警: 已启用 ({len(alerts)} 条规则)")
    else:
        print(f"[启动] 价格告警: 未配置")
    if notification_email:
        print(f"[启动] 系统通知目标: {notification_email}")
    print(f"[启动] 按 Ctrl+C 停止")

    # ── 新浪财经境外VPS连通性检测 ──
    _test_sina_connectivity(products)

    print()

    iteration = 0
    last_csv_time = 0.0
    last_scheduler_check = 0.0
    last_sina_stats_report = 0.0
    last_csv_record_time = 0.0          # 最后一次成功写入 CSV 的时间戳
    archive_pending = False             # 休市后等待 20 分钟触发归档
    market_closed_at_time = 0.0         # 检测到休市的时间点

    # ── 启动时检查是否需要立即归档（程序重启场景） ──
    if not once and config["archive"]["enabled"]:
        now_beijing = datetime.now(BEIJING_TZ)
        if _is_market_closed(now_beijing) and _state_should_run("weekly_archive", now_beijing.date().isoformat()):
            market_close_minute = 20     # 美国休市在 04:20(夏)/05:20(冬)
            us_dst = _is_us_dst()
            close_hour = 4 if us_dst else 5
            market_close_today = now_beijing.replace(hour=close_hour, minute=market_close_minute, second=0, microsecond=0)
            if now_beijing >= market_close_today + timedelta(minutes=20):
                print(f"[启动] 检测到休市中且已过 CSV 停录后 20 分钟，触发归档 ...")
                archive_pending = True
                market_closed_at_time = time.time()  # 立即触发
                last_csv_record_time = time.time() - 1300  # 模拟 20+ 分钟前最后一次记录

    try:
        while not _shutdown_event.is_set():
            iteration += 1
            loop_start = time.monotonic()

            # 1. 获取所有产品价格
            prices = fetch_all_prices(products, max_workers)

            # 2. 输出到终端
            clear = not once
            print_price_snapshot(prices, products, iteration, clear_screen=clear)

            # 3. CSV 记录（每 csv_interval 秒一次；休市时只获取不记录）
            now = time.time()
            if not once and now - last_csv_time >= csv_interval:
                timestamp = datetime.now(BEIJING_TZ)
                market_closed_now = _is_market_closed(timestamp)
                if market_closed_now:
                    print(f"  [待命] 全球股市休市中，跳过 CSV 记录（{timestamp.strftime('%H:%M:%S')}）")
                    if not archive_pending:
                        archive_pending = True
                        market_closed_at_time = now
                        print(f"  [归档] 检测到休市，将在 CSV 停止录入 20 分钟后自动归档")
                else:
                    write_csv_row(csv_dir, products, prices, timestamp)
                    print(f"  [CSV] 已记录到 {_get_csv_path(csv_dir, timestamp).name}")
                    last_csv_record_time = now
                    if archive_pending:
                        archive_pending = False
                last_csv_time = now

            # 4. 检查价格告警
            if not once and alerts:
                check_and_send_alerts(notify_cfg, smtp_cfg, alerts, products, prices, alert_states)

            # 5. 境外VPS监控：每5分钟输出新浪请求统计
            if not once and now - last_sina_stats_report >= 300:
                summary = _get_sina_stats_summary()
                if _sina_consecutive_failures >= 3:
                    print(f"  [网络] {summary}", file=sys.stderr)
                else:
                    print(f"  {summary}")
                last_sina_stats_report = now

            # 6. 归档检查：CSV 停止录入 20 分钟后自动触发
            if not once and archive_pending and now - market_closed_at_time >= 1200:
                current_week_str = _iso_week_str(datetime.now(BEIJING_TZ).date())
                print(f"  [归档] CSV 已停录 20 分钟，第 {current_week_str} 周归档触发")
                archiver.check_and_run(notify_cfg, smtp_cfg)
                archive_pending = False

            # 7. 检查定时任务（每分钟检查一次，避免过于频繁）
            if not once and now - last_scheduler_check >= 60:
                cleaner.check_and_run(notify_cfg, smtp_cfg)
                _check_log_size(log_path)
                last_scheduler_check = now

            # 8. 单次执行模式
            if once:
                break

            # 9. 等待下一轮
            elapsed = time.monotonic() - loop_start
            wait_time = max(poll_interval - elapsed, 0)
            _shutdown_event.wait(wait_time)

    except KeyboardInterrupt:
        pass
    finally:
        print("\n[退出] 监控已停止。")


# ═══════════════════════════════════════════════════════════════════════════════
# Linux 优化
# ═══════════════════════════════════════════════════════════════════════════════

def _linux_daemonize(pid_file: Optional[Path] = None) -> None:
    """Linux 守护进程化（双 fork 方式）。"""
    if not IS_LINUX:
        print("[警告] 守护进程模式仅在 Linux 下可用")
        return

    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    os.setsid()
    os.umask(0o022)

    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    os.chdir("/")

    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, sys.stdin.fileno())
    os.dup2(devnull, sys.stdout.fileno())
    os.dup2(devnull, sys.stderr.fileno())
    os.close(devnull)

    if pid_file:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()))


def _linux_set_resource_limits() -> None:
    """设置 Linux 资源限制（内存、文件描述符）。"""
    if not IS_LINUX:
        return
    try:
        import resource
        mem_limit = 512 * 1024 * 1024  # 512MB
        resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
    except (ImportError, ValueError, OSError):
        pass


def _linux_set_cpu_affinity() -> None:
    """将进程绑定到特定 CPU 核心以减少上下文切换。"""
    if not IS_LINUX:
        return
    try:
        cpu_count = os.cpu_count() or 1
        target_cpu = {cpu_count - 1}
        os.sched_setaffinity(0, target_cpu)
    except (AttributeError, OSError):
        pass


def _setup_signal_handlers() -> None:
    """注册信号处理函数以实现优雅退出。"""
    def _handler(signum, frame):
        if _shutdown_event.is_set():
            sys.exit(1)
        print(f"\n[信号] 收到 {signal.Signals(signum).name}，正在退出...")
        _shutdown_event.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    if IS_LINUX:
        def _hup_handler(signum, frame):
            print("[信号] 收到 SIGHUP — 重新加载配置文件需重启程序")
        signal.signal(signal.SIGHUP, _hup_handler)


def _ensure_single_instance(pid_file: Path) -> bool:
    """确保只有一个实例在运行（通过 PID 文件）。"""
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, 0)
            print(f"[错误] 已有实例在运行 (PID: {old_pid})", file=sys.stderr)
            return False
        except (ValueError, OSError):
            pid_file.unlink(missing_ok=True)

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════════

def _die(msg: str) -> None:
    """输出错误信息并退出。"""
    print(f"[致命错误] {msg}", file=sys.stderr)
    sys.exit(1)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="轻量级金融产品实时价格监控程序",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python price_monitor.py                         前台运行（默认）
  python price_monitor.py --daemon                Linux 守护进程模式
  python price_monitor.py --once                  仅获取一次价格
  python price_monitor.py --archive-now           立即归档本周数据并发送通知
  python price_monitor.py --test-telegram         测试 TeleGram Bot 连通性
  python price_monitor.py --test-telegram-file ./test.zip  测试文件发送
  python price_monitor.py --config my.json        使用自定义配置
        """,
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="主配置文件路径（默认: monitor_config.json）",
    )
    parser.add_argument(
        "--smtp-config", type=str, default=None,
        help="SMTP 配置文件路径（默认: smtp_config.json）",
    )
    parser.add_argument(
        "--alerts-config", type=str, default=None,
        help="告警配置文件路径（默认: alerts_config.json）",
    )
    parser.add_argument(
        "--csv-dir", type=str, default=None,
        help="CSV 输出目录（覆盖配置文件中的设置）",
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Linux 下以守护进程运行",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="仅获取一次价格并输出，不循环",
    )
    parser.add_argument(
        "--test-telegram", action="store_true",
        help="单次测试 Telegram Bot 连通性（需先配置 monitor_config.json 中的 telegram 参数）",
    )
    parser.add_argument(
        "--test-telegram-file", type=str, default=None, metavar="FILE",
        help="单次测试 Telegram 文件发送功能（需指定本地文件路径）",
    )
    parser.add_argument(
        "--archive-now", action="store_true",
        help="立即手动触发本周数据归档（绕过定时调度，用于测试归档+通知流程）",
    )
    parser.add_argument(
        "--pid-file", type=str, default=None,
        help="PID 文件路径（默认: /tmp/price_monitor.pid）",
    )
    return parser.parse_args()


def main() -> None:
    _configure_console_streams()
    args = parse_args()

    # ── 解析配置文件路径 ──
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    smtp_path = Path(args.smtp_config) if args.smtp_config else DEFAULT_SMTP_PATH
    alerts_path = Path(args.alerts_config) if args.alerts_config else DEFAULT_ALERTS_PATH

    # ── 加载配置 ──
    config = load_monitor_config(config_path)
    smtp_cfg = load_smtp_config(smtp_path)
    alerts = load_alerts_config(alerts_path)

    # ── 日志文件设置（需在配置加载后，以便读取 log_file 配置项） ──
    log_path = Path(config["log_file"]) if config.get("log_file") else LOG_FILE_PATH
    log_path = log_path.resolve()
    _setup_log_file(log_path)
    print(f"[启动] 日志文件: {log_path}")

    # ── Telegram 连通性测试模式 ──
    if args.test_telegram:
        _test_telegram_communication(config["notification"])
        sys.exit(0)

    # ── Telegram 文件发送测试模式 ──
    if args.test_telegram_file:
        _test_telegram_file_send(config["notification"], Path(args.test_telegram_file))
        sys.exit(0)

    # ── CSV 输出目录 ──
    if args.csv_dir:
        csv_dir = Path(args.csv_dir)
    elif config.get("csv_output_dir"):
        csv_dir = Path(config["csv_output_dir"])
    else:
        csv_dir = DEFAULT_CSV_DIR
    csv_dir = csv_dir.resolve()
    csv_dir.mkdir(parents=True, exist_ok=True)

    # ── 手动归档模式 ──
    if args.archive_now:
        archiver = WeeklyArchiver(config["archive"], csv_dir, config["products"])
        ok = archiver.force_run(config["notification"], smtp_cfg)
        if not ok:
            print("[归档-手动] 未生成归档文件（无 CSV 数据或执行出错）")
        sys.exit(0 if ok else 1)

    # ── 预创建必要目录 ──
    archive_dir = (
        Path(config["archive"].get("output_dir"))
        if config["archive"].get("output_dir")
        else DEFAULT_ARCHIVE_DIR
    )
    archive_dir.mkdir(parents=True, exist_ok=True)

    # ── PID 文件 ──
    pid_file = None
    if args.pid_file:
        pid_file = Path(args.pid_file)
    elif args.daemon and IS_LINUX:
        pid_file = Path("/tmp/price_monitor.pid")

    # ── 信号处理 ──
    _setup_signal_handlers()

    # ── 守护进程模式 ──
    if args.daemon and IS_LINUX:
        if pid_file and not _ensure_single_instance(pid_file):
            sys.exit(1)
        print(f"[守护进程] 正在后台启动，PID 文件: {pid_file}")
        _linux_daemonize(pid_file)

    # ── Linux 特定优化 ──
    if IS_LINUX:
        _linux_set_resource_limits()
        _linux_set_cpu_affinity()

    # ── 启动监控 ──
    try:
        run_monitor_loop(config, smtp_cfg, alerts, csv_dir, once=args.once, log_path=log_path)
    finally:
        if pid_file and pid_file.exists():
            with suppress(OSError):
                pid_file.unlink()


if __name__ == "__main__":
    main()
