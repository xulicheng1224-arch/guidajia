"""Launch only the monitor's dedicated Taobao browser with a direct connection.

Terminal proxy variables can differ from macOS network settings and may point
to a proxy that is no longer running. Never change the user's system settings.
"""

import os


def taobao_context_options():
    """Keep login and collection consistent, using the installed browser's UA."""
    return {"locale": "zh-CN", "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1440, "height": 900}}


def taobao_browser_options(channel):
    proxy_keys = {"http_proxy", "https_proxy", "all_proxy"}
    options = {
        "args": ["--no-proxy-server"],
        "env": {
            key: value
            for key, value in os.environ.items()
            if key.lower() not in proxy_keys
        },
    }
    if channel != "chromium":
        options["channel"] = channel
    return options
