"""CloakBrowser options and official network-identity diagnostics."""
from __future__ import annotations

import asyncio
import hashlib
import sys
import time

from .config import ProxyConfig

PLATFORM_OPTIONS = [
    {"value": "auto", "label": "自动（推荐）"},
    {"value": "windows", "label": "Windows"},
    {"value": "macos", "label": "macOS"},
]

RELEASE_CHANNELS = [
    {"value": "stable", "label": "Stable（稳定版）"},
    {"value": "preview", "label": "Preview（预览版）"},
]

TIMEZONE_OPTIONS = [
    {"value": "Asia/Shanghai", "label": "中国大陆（上海/北京）"},
    {"value": "Asia/Hong_Kong", "label": "中国香港"},
    {"value": "Asia/Taipei", "label": "中国台湾（台北）"},
    {"value": "Asia/Tokyo", "label": "日本（东京）"},
    {"value": "Asia/Seoul", "label": "韩国（首尔）"},
    {"value": "Asia/Singapore", "label": "新加坡"},
    {"value": "Asia/Bangkok", "label": "泰国（曼谷）"},
    {"value": "Asia/Ho_Chi_Minh", "label": "越南（胡志明市）"},
    {"value": "Asia/Kuala_Lumpur", "label": "马来西亚（吉隆坡）"},
    {"value": "Asia/Jakarta", "label": "印度尼西亚（雅加达）"},
    {"value": "Asia/Kolkata", "label": "印度（加尔各答时区）"},
    {"value": "Asia/Dubai", "label": "阿联酋（迪拜）"},
    {"value": "Europe/London", "label": "英国（伦敦）"},
    {"value": "Europe/Paris", "label": "法国（巴黎）"},
    {"value": "Europe/Berlin", "label": "德国（柏林）"},
    {"value": "Europe/Madrid", "label": "西班牙（马德里）"},
    {"value": "Europe/Rome", "label": "意大利（罗马）"},
    {"value": "Europe/Amsterdam", "label": "荷兰（阿姆斯特丹）"},
    {"value": "Europe/Moscow", "label": "俄罗斯（莫斯科）"},
    {"value": "America/New_York", "label": "美国东部（纽约）"},
    {"value": "America/Chicago", "label": "美国中部（芝加哥）"},
    {"value": "America/Denver", "label": "美国山地（丹佛）"},
    {"value": "America/Los_Angeles", "label": "美国西部（洛杉矶）"},
    {"value": "America/Toronto", "label": "加拿大东部（多伦多）"},
    {"value": "America/Vancouver", "label": "加拿大西部（温哥华）"},
    {"value": "America/Sao_Paulo", "label": "巴西（圣保罗）"},
    {"value": "Australia/Sydney", "label": "澳大利亚（悉尼）"},
    {"value": "Australia/Melbourne", "label": "澳大利亚（墨尔本）"},
    {"value": "Pacific/Auckland", "label": "新西兰（奥克兰）"},
]

LOCALE_OPTIONS = [
    {"value": "zh-CN", "label": "简体中文（中国大陆）"},
    {"value": "zh-HK", "label": "繁体中文（中国香港）"},
    {"value": "zh-TW", "label": "繁体中文（中国台湾）"},
    {"value": "en-US", "label": "英语（美国）"},
    {"value": "en-GB", "label": "英语（英国）"},
    {"value": "en-CA", "label": "英语（加拿大）"},
    {"value": "en-AU", "label": "英语（澳大利亚）"},
    {"value": "en-SG", "label": "英语（新加坡）"},
    {"value": "en-IN", "label": "英语（印度）"},
    {"value": "ja-JP", "label": "日语（日本）"},
    {"value": "ko-KR", "label": "韩语（韩国）"},
    {"value": "de-DE", "label": "德语（德国）"},
    {"value": "fr-FR", "label": "法语（法国）"},
    {"value": "es-ES", "label": "西班牙语（西班牙）"},
    {"value": "it-IT", "label": "意大利语（意大利）"},
    {"value": "nl-NL", "label": "荷兰语（荷兰）"},
    {"value": "pt-BR", "label": "葡萄牙语（巴西）"},
    {"value": "pt-PT", "label": "葡萄牙语（葡萄牙）"},
    {"value": "ru-RU", "label": "俄语（俄罗斯）"},
    {"value": "id-ID", "label": "印度尼西亚语"},
    {"value": "th-TH", "label": "泰语（泰国）"},
    {"value": "vi-VN", "label": "越南语（越南）"},
    {"value": "ms-MY", "label": "马来语（马来西亚）"},
    {"value": "tr-TR", "label": "土耳其语（土耳其）"},
    {"value": "ar-AE", "label": "阿拉伯语（阿联酋）"},
]


def default_platform() -> str:
    return "macos" if sys.platform == "darwin" else "windows"


def host_platform_label() -> str:
    return "macOS" if sys.platform == "darwin" else ("Windows" if sys.platform == "win32" else "Linux")


def region_label_for_timezone(timezone: str | None) -> str | None:
    if not timezone:
        return None
    match = next((item for item in TIMEZONE_OPTIONS if item["value"] == timezone), None)
    return match["label"] if match else timezone


def projected_user_agent(
    platform: str,
    browser_version: str | None,
    brand_version: str | None = None,
) -> str | None:
    """Build the reduced UA expected from the configured platform and version."""
    source_version = brand_version or browser_version
    if not source_version:
        return None
    ua_version = brand_version or f"{source_version.split('.', 1)[0]}.0.0.0"
    resolved_platform = default_platform() if platform == "auto" else platform
    platform_token = (
        "Macintosh; Intel Mac OS X 10_15_7"
        if resolved_platform == "macos"
        else "Windows NT 10.0; Win64; x64"
    )
    return (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{ua_version} Safari/537.36"
    )


def browser_binary_info(
    release_channel: str = "stable", browser_version: str | None = None
) -> dict:
    """Return the official resolved binary metadata with a stable public shape."""
    import cloakbrowser

    info = cloakbrowser.binary_info(
        release_channel=release_channel,
        browser_version=browser_version,
    )
    return {
        "version": info.get("version"),
        "tier": info.get("tier"),
        "bundledVersion": info.get("bundled_version"),
        "platform": info.get("platform"),
        "binaryPath": info.get("binary_path"),
        "installed": bool(info.get("installed")),
        "releaseChannel": release_channel,
        "requestedVersion": browser_version,
    }


def proxy_signature(proxy: ProxyConfig | None) -> str:
    if proxy is None:
        source = "direct"
    else:
        # Do not persist a password-derived hash. Presence is enough to make a
        # credential-mode change invalidate the diagnostic snapshot.
        source = f"{proxy.server}|{proxy.username or ''}|{proxy.has_password}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


async def resolve_launch_identity(
    proxy: ProxyConfig | None,
    timezone_override: str | None = None,
    locale_override: str | None = None,
) -> dict:
    """Resolve the final launch identity exactly once before Chromium starts."""
    import cloakbrowser

    try:
        timezone, locale, exit_ip = await asyncio.to_thread(
            cloakbrowser.maybe_resolve_geoip,
            True,
            proxy.cloak_value() if proxy else None,
            timezone_override,
            locale_override,
            None,
        )
    except ImportError as exc:
        raise RuntimeError(
            "缺少 CloakBrowser GeoIP 依赖，请安装：pip install -e '.[dev]'"
        ) from exc

    if not timezone or not locale:
        raise RuntimeError("无法解析完整的网络身份（时区和语言）")
    if proxy and not exit_ip:
        raise RuntimeError("无法解析代理的公网出口 IP")
    return {"timezone": timezone, "locale": locale, "exitIp": exit_ip}


async def probe_network_identity(
    proxy: ProxyConfig | None,
    timezone_override: str | None = None,
    locale_override: str | None = None,
) -> dict:
    """Use CloakBrowser's own GeoIP path so diagnostics match real launches."""
    import cloakbrowser

    started = time.perf_counter()
    try:
        timezone, locale, exit_ip = await asyncio.to_thread(
            cloakbrowser.maybe_resolve_geoip,
            True,
            proxy.cloak_value() if proxy else None,
            None,
            None,
            None,
        )
    except ImportError as exc:
        raise RuntimeError(
            "缺少 CloakBrowser GeoIP 依赖，请安装：pip install -e '.[dev]'"
        ) from exc
    if not exit_ip:
        target = "代理" if proxy else "直接连接"
        raise RuntimeError(f"无法解析{target}的公网出口 IP")
    if not timezone or not locale:
        raise RuntimeError("已获得出口 IP，但无法解析对应的时区或语言")
    latency_ms = round((time.perf_counter() - started) * 1000)
    return {
        "ok": True,
        "exitIp": exit_ip,
        "detectedTimezone": timezone,
        "detectedLocale": locale,
        "appliedTimezone": timezone_override or timezone,
        "appliedLocale": locale_override or locale,
        "timezoneSource": "custom" if timezone_override else "auto",
        "localeSource": "custom" if locale_override else "auto",
        "webrtcIp": exit_ip,
        "latencyMs": latency_ms,
        "proxySignature": proxy_signature(proxy),
    }
