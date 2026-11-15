"""A tiny OpenAI-compatible VLM stand-in.

Purpose: make the whole pipeline testable and demo-able without a paid API key
or a 20 GB local model download. It is *not* a fake success path — it really
reads the probe image (nearest-colour sampling) and really parses the frame
metadata to produce a plausible answer, so a broken pipeline still fails.

What it does not do: understand an actual screenshot. Analysis output from the
mock is marked ``"mock": true`` and the CLI says so.
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from .probe import CELL, GAP, MARGIN, N_CELLS, read_challenge_colors

CHALLENGE_SIZE = (MARGIN * 2 + N_CELLS * CELL + (N_CELLS - 1) * GAP, MARGIN * 2 + CELL)

FRAME_LINE = re.compile(r"^\[?帧\s*(\d+)(?:\s*[-–—~]\s*\d+)?\]?\s*(.*)$")
#: New-format metadata has the display between the time and the app:
#: ``[帧 3] 10:20:00 | m2 | code | main.py``.
SCREEN_FIELD = re.compile(r"^(?:m\d+|屏\d*|\?)$")
APP_CATEGORY = {
    "code": "coding",
    "pycharm": "coding",
    "idea": "coding",
    "devenv": "coding",
    "terminal": "coding",
    "chrome": "browsing",
    "msedge": "browsing",
    "firefox": "browsing",
    "wechat": "communication",
    "feishu": "communication",
    "slack": "communication",
    "teams": "meeting",
    "zoom": "meeting",
    "figma": "design",
    "potplayer": "media",
    "vlc": "media",
}


def _guess_category(app: str) -> str:
    a = (app or "").lower()
    for key, cat in APP_CATEGORY.items():
        if key in a:
            return cat
    return "other"


def _categories(app: str) -> str:
    return _guess_category(app)


@dataclass
class MockRequest:
    images: list[bytes]
    text: str


class MockVLM:
    """Start/stop a threaded HTTP server speaking ``/v1/chat/completions``."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        confidence: float = 0.6,
        summary_style: str = "full",
    ) -> None:
        self.host = host
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.daemon_threads = True
        # Knobs the tests use to simulate a weak model vs a strong one.
        self._server.confidence = confidence  # type: ignore[attr-defined]
        self._server.summary_style = summary_style  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.requests = 0

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def start(self) -> MockVLM:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3.0)

    def __enter__(self) -> MockVLM:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _decode_image(part: dict) -> bytes | None:
    url = ""
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        url = image_url.get("url", "")
    elif isinstance(image_url, str):
        url = image_url
    if not url.startswith("data:"):
        return None
    try:
        return base64.b64decode(url.split(",", 1)[1])
    except (IndexError, ValueError):
        return None


def _extract(body: dict) -> MockRequest:
    images: list[bytes] = []
    texts: list[str] = []
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            if message.get("role") == "user":
                texts.append(content)
            continue
        for part in content or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                data = _decode_image(part)
                if data:
                    images.append(data)
    return MockRequest(images=images, text="\n".join(texts))


def _is_challenge(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size == CHALLENGE_SIZE
    except Exception:
        return False


def _answer_probe(data: bytes) -> str:
    colors = read_challenge_colors(data)
    return json.dumps({"colors": colors}, ensure_ascii=False)


def parse_frame_line(line: str) -> tuple[int, str, str] | None:
    """Read one metadata line, with or without the display field.

    Accepted shapes (the model only sees text, so both must keep working):
    ``[帧 3] 10:20:00 | code | main.py``
    ``[帧 3] 10:20:00 | m2 | code | main.py``
    ``帧 12–17 | 18:34:26–18:36:25 | m2 | gameviewer | 华硕主机``
    """
    match = FRAME_LINE.match(line.strip())
    if not match:
        return None
    index = int(match.group(1))
    fields = [part.strip() for part in match.group(2).split("|")]
    if fields and not fields[0]:
        fields.pop(0)  # "帧 3 | time | app | title" leaves an empty first field
    if len(fields) >= 2 and SCREEN_FIELD.match(fields[1]):
        app = fields[2] if len(fields) >= 3 else "unknown"
        title = fields[3] if len(fields) >= 4 else ""
    else:
        app = fields[1] if len(fields) >= 3 else "unknown"
        title = fields[2] if len(fields) >= 3 else (fields[-1] if fields else "")
    return index, app, title


def _answer_frames(
    text: str, image_count: int, *, confidence: float = 0.6, summary_style: str = "full"
) -> str:
    metas = []
    for line in text.splitlines():
        parsed = parse_frame_line(line)
        if parsed:
            idx, app, title = parsed
            metas.append({"idx": idx, "app": app, "title": title})
    if not metas:
        metas = [{"idx": i, "app": "unknown", "title": ""} for i in range(max(1, image_count))]

    last = metas[-1]["idx"]
    mid = metas[len(metas) // 2]["idx"] if len(metas) > 1 else last
    first_app = metas[0]["app"]
    second_app = metas[len(metas) // 2]["app"] if len(metas) > 1 else first_app

    def summary_for(app: str, title: str) -> str:
        if summary_style == "shallow":
            # The shape of a model that produced structure without understanding.
            return app or "?"
        return f"（mock 分析）在 {app} 里处理窗口「{title[:40]}」"

    activities = []
    if mid > 0:
        activities.append(
            {
                "from_frame": 0,
                "to_frame": mid - 1,
                "app": first_app,
                "category": _categories(first_app),
                "title": metas[0]["title"] or "（mock）",
                "summary": summary_for(first_app, metas[0]["title"]),
                "confidence": confidence,
            }
        )
    activities.append(
        {
            "from_frame": mid,
            "to_frame": last,
            "app": second_app,
            "category": _categories(second_app),
            "title": metas[-1]["title"] or "（mock）",
            "summary": summary_for(second_app, metas[-1]["title"]),
            "confidence": confidence,
        }
    )
    return json.dumps({"activities": activities, "mock": True}, ensure_ascii=False)


def _answer_rollup(text: str) -> str:
    match = re.search(r"总记录时长：(\S+)", text)
    duration = match.group(1) if match else "一段时间"
    return json.dumps(
        {
            "narrative": (
                f"（mock 叙述）这一天一共记录了 {duration}，中间有几段连续的工作。"
                "接上真实模型后，这里会是它写的复盘文字。"
            ),
            "highlights": ["（mock）这里会是 1~3 个值得注意的点"],
            "suggestions": ["（mock）这里会是建议"],
        },
        ensure_ascii=False,
    )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # silence
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._send(200, {"object": "list", "data": [{"id": "mock-vlm", "object": "model"}]})
        else:
            self._send(404, {"error": {"message": f"unknown path {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": f"unknown path {self.path}"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid json"}})
            return

        request = _extract(body)
        if request.images and _is_challenge(request.images[0]):
            content = _answer_probe(request.images[0])
            finish = "stop"
        elif request.images:
            content = _answer_frames(
                request.text,
                len(request.images),
                confidence=getattr(self.server, "confidence", 0.6),
                summary_style=getattr(self.server, "summary_style", "full"),
            )
            finish = "stop"
        elif "总记录时长" in request.text:
            content = _answer_rollup(request.text)
            finish = "stop"
        else:
            content = json.dumps({"ok": True, "note": "mock vlm is reachable"}, ensure_ascii=False)
            finish = "stop"

        prompt_tokens = 40 + 320 * len(request.images) + len(request.text) // 4
        completion_tokens = max(1, len(content) // 4)
        self._send(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "mock-vlm"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": finish,
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
        )
