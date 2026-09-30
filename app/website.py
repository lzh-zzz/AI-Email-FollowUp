"""Bounded public HTML reading; no JavaScript, cookies, login or arbitrary crawl."""

import http.client
import ipaddress
import re
import socket
import ssl
import time
import zlib
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

MAX_PAGE_BYTES = 1024 * 1024
MAX_TEXT = 4000


class WebsiteError(Exception):
    pass


def checked_url(url):
    try:
        if len(url) > 2000 or any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
            raise ValueError()
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port != (443 if parsed.scheme == "https" else 80):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or "%" in host or host.endswith((".local", ".localhost")) or host == "localhost":
            raise ValueError()
        authority = f"[{host}]" if ":" in host else host
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parsed.query, safe="%:@!$&'()*+,;=/?-._~")
        return urlunsplit((parsed.scheme, authority, path, query, ""))
    except (ValueError, UnicodeError):
        raise WebsiteError("官网需使用公开 HTTP/HTTPS 地址及默认端口，不能包含账号或内网目标。") from None


def public_addresses(host, port):
    try:
        addresses = list(
            dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        )
    except OSError:
        raise WebsiteError("官网域名解析失败，请检查网址和网络。") from None
    if not addresses or any(
        not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses
    ):
        raise WebsiteError("仅支持公开网站，不能读取本机、内网或保留地址。")
    return addresses


def request_page(url, deadline):
    """Pin the connection to a validated public IP, retaining Host and TLS hostname checks."""
    parsed = urlsplit(url)
    host = parsed.hostname
    port = 443 if parsed.scheme == "https" else 80
    addresses = public_addresses(host, port)
    for ip in addresses[:2]:
        connection = http.client.HTTPConnection(host, port, timeout=8)
        response = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebsiteError("官网读取超时，可重试或使用手动背景。")
            connection.sock = socket.create_connection((ip, port), timeout=min(8, remaining))
            if parsed.scheme == "https":
                context = ssl.create_default_context()
                context.set_alpn_protocols(["http/1.1"])
                connection.sock = context.wrap_socket(connection.sock, server_hostname=host)
            stream = connection.sock
            connection.request(
                "GET",
                parsed.path + ("?" + parsed.query if parsed.query else ""),
                headers={
                    "Host": parsed.netloc,
                    "User-Agent": "PackPilotDemo/1.0 (company background reader)",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "gzip, deflate",
                },
            )
            response = connection.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                return response.status, headers, b""
            if response.status != 200:
                raise WebsiteError(f"官网返回 HTTP {response.status}；无法读取时请手动补充背景。")
            if headers.get("content-type", "").split(";")[0].strip().lower() not in {
                "text/html",
                "application/xhtml+xml",
            }:
                raise WebsiteError("该链接不是 HTML 网页，请填写公司官网页面。")
            encoding = headers.get("content-encoding", "identity").lower()
            if encoding not in {"", "identity", "gzip", "deflate"}:
                raise WebsiteError("网站返回了不支持的压缩内容，请手动补充背景。")
            decoder = (
                zlib.decompressobj(31 if encoding == "gzip" else 15)
                if encoding in {"gzip", "deflate"}
                else None
            )
            length = headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_PAGE_BYTES:
                raise WebsiteError("网页超过 1MB 读取上限，请使用更简洁的公司介绍页面。")
            chunks, total, decoded_total = [], 0, 0
            while total <= MAX_PAGE_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebsiteError("官网读取超时，可重试或使用手动背景。")
                stream.settimeout(min(8, remaining))
                chunk = response.read1(min(65536, MAX_PAGE_BYTES + 1 - total))
                if not chunk:
                    if decoder and not decoder.eof:
                        raise WebsiteError("网页压缩数据不完整，请重试或手动补充背景。")
                    return response.status, headers, b"".join(chunks)
                total += len(chunk)
                if decoder:
                    try:
                        chunk = decoder.decompress(chunk, MAX_PAGE_BYTES + 1 - decoded_total)
                    except zlib.error:
                        raise WebsiteError("网页压缩数据异常，请重试或手动补充背景。") from None
                decoded_total += len(chunk)
                if decoded_total > MAX_PAGE_BYTES:
                    raise WebsiteError("网页解压后超过 1MB 读取上限，请使用更简洁的介绍页面。")
                chunks.append(chunk)
            raise WebsiteError("网页超过 1MB 读取上限，请使用更简洁的公司介绍页面。")
        except WebsiteError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            continue
        finally:
            if response:
                response.close()
            connection.close()
    raise WebsiteError("官网连接失败或超时；网站可能限制访问，请手动补充背景。")


class VisibleHTML(HTMLParser):
    ignored = {"script", "style", "nav", "header", "footer", "form", "svg", "template", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.suppressed = []
        self.links = []
        self.anchor = None
        self.title = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.anchor = {"href": attrs.get("href", ""), "text": ""}
        if tag == "title":
            self.in_title = True
        if tag in self.ignored:
            self.suppressed.append(tag)
        if not self.suppressed and tag in {"p", "div", "li", "h1", "h2", "h3", "br"}:
            self.blocks.append("\n")

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor:
            self.links.append(self.anchor)
            self.anchor = None
        if tag == "title":
            self.in_title = False
        if tag in self.suppressed:
            index = len(self.suppressed) - 1 - self.suppressed[::-1].index(tag)
            del self.suppressed[index:]
        if not self.suppressed and tag in {"p", "div", "li", "h1", "h2", "h3"}:
            self.blocks.append("\n")

    def handle_data(self, data):
        if self.anchor:
            self.anchor["text"] += data
        if self.in_title:
            self.title.append(data)
        if not self.suppressed and not self.in_title:
            self.blocks.append(data)

    def text(self):
        return "\n".join(
            line for part in "".join(self.blocks).splitlines() if (line := " ".join(part.split()))
        )[:MAX_TEXT]


class WebsiteReader:
    def __init__(self, request=request_page):
        self.request = request

    def _page(self, url, deadline, allowed_host=None):
        for _ in range(4):
            url = checked_url(url)
            if allowed_host and urlsplit(url).hostname != allowed_host:
                raise WebsiteError("公司介绍链接跳转到其他网站，已跳过。")
            status, headers, body = self.request(url, deadline)
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location:
                    raise WebsiteError("网页重定向没有目标地址。")
                url = urljoin(url, location)
                continue
            if status != 200:
                raise WebsiteError(f"官网返回 HTTP {status}，无法读取。")
            if len(body) > MAX_PAGE_BYTES:
                raise WebsiteError("网页超过 1MB 读取上限。")
            charset = re.search(r"charset=[\"']?([\w-]+)", headers.get("content-type", ""), re.I)
            if not charset:
                charset = re.search(rb"charset=[\"']?([\w-]+)", body[:4096], re.I)
            encoding = charset[1] if charset else "utf-8"
            if isinstance(encoding, bytes):
                encoding = encoding.decode("ascii")
            try:
                html = body.decode(encoding, errors="replace")
            except LookupError:
                html = body.decode("utf-8", errors="replace")
            parser = VisibleHTML()
            parser.feed(html)
            text = parser.text()
            if len(text) < 80:
                raise WebsiteError("网页可读正文不足，可能需要 JavaScript 或登录；请手动补充背景。")
            if re.search(
                r"verify you are human|checking your browser|just a moment|enable javascript and cookies|access denied|captcha",
                text[:800],
                re.I,
            ):
                raise WebsiteError("网站要求验证或限制自动访问；请手动补充背景。")
            return {"url": url, "title": " ".join(parser.title)[:200], "text": text}, parser.links
        raise WebsiteError("官网重定向次数过多，请填写最终官网地址。")

    def read(self, url):
        deadline = time.monotonic() + 30
        home, links = self._page(url, deadline)
        pages, warnings = [home], []
        host = urlsplit(home["url"]).hostname
        for link in links:
            if not re.search(
                r"\babout\b|our[ -]?story|关于我们|公司介绍", link["text"] + " " + link["href"], re.I
            ):
                continue
            try:
                target = checked_url(urljoin(home["url"], link["href"]))
            except WebsiteError:
                continue
            if urlsplit(target).hostname != host or target == home["url"]:
                continue
            try:
                about, _ = self._page(target, deadline, allowed_host=host)
                if about["url"] != home["url"]:
                    about["text"] = about["text"][:3000]
                    pages.append(about)
            except WebsiteError as exc:
                warnings.append(str(exc))
            break
        return {"pages": pages, "warnings": warnings}
