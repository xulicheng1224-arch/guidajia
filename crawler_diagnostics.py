"""Small page-render evidence: counts and fixed signals, never raw page data."""

import asyncio
import re
from urllib.parse import urlsplit


class PageDiagnostics:
    def __init__(self, page):
        self.page = page
        self.failures, self.http_errors, self.script_errors = [], [], []
        page.on("requestfailed", self.failed)
        page.on("response", self.response)
        page.on("pageerror", self.script_error)
        page.on("console", self.console_error)

    def failed(self, request):
        if len(self.failures) >= 12:
            return
        code = re.search(r"\bERR_[A-Z0-9_]+\b", request.failure or "")
        self.failures.append({"host": urlsplit(request.url).hostname,
                              "resource_type": request.resource_type,
                              "code": code[0] if code else "network_failed"})

    def response(self, response):
        if response.status >= 400 and len(self.http_errors) < 12:
            self.http_errors.append({"host": urlsplit(response.url).hostname,
                                     "resource_type": response.request.resource_type,
                                     "status": response.status})

    def script_error(self, error):
        if len(self.script_errors) < 12:
            # Error text can contain credentials or user data. Keep only type.
            kind = re.search(r"\b(?:Type|Reference|Syntax|Range|URI|Eval)Error\b", str(error))
            self.script_errors.append(kind[0] if kind else "ScriptError")

    def console_error(self, message):
        if message.type != "error" or len(self.script_errors) >= 12:
            return
        # Classify fixed browser errors without saving URLs, tokens or text.
        text = message.text.lower()
        if "cors policy" in text or "cross-origin request blocked" in text:
            self.script_errors.append("CORSBlocked")
        elif "content security policy" in text:
            self.script_errors.append("CSPBlocked")
        elif "failed to load resource" in text:
            self.script_errors.append("ResourceLoadError")

    async def capture(self, result):
        info = result.setdefault("diagnostics", {})
        info.update(request_failures=list(self.failures), resource_http_errors=list(self.http_errors),
                    script_error_types=list(self.script_errors))
        try:
            evidence = await asyncio.wait_for(self.page.evaluate("""() => {
                const body = document.body;
                const text = body?.innerText || '';
                return {document_ready_state:document.readyState, title:document.title || '',
                    body_text_length:text.length, dom_element_count:document.querySelectorAll('*').length,
                    script_count:document.scripts.length, iframe_count:document.querySelectorAll('iframe').length,
                    page_signals:['验证码拦截','安全验证','请完成验证','拖动滑块','请按住滑块','请登录','加载中','网络异常']
                        .filter(s => document.title.includes(s) || text.includes(s))};
            }"""), timeout=2)
            title = evidence.pop("title")[:300]
            result["title"] = title
            info.update(evidence, title=title)
            info["render_state"] = (
                "blank" if not title.strip() and not evidence["body_text_length"]
                else "loading" if evidence["document_ready_state"] == "loading"
                else "content"
            )
        except Exception:
            info["page_diagnostics_unavailable"] = True
        return info
