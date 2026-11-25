#!/usr/bin/env python3
"""轻量免 Cookie 抖音无水印下载器（纯 Python 标准库实现，适配 Termux）。

基于 QingZai（轻载）验证过的解析思路：
1. 伪装移动端 Safari 请求 iesdouyin.com 分享页 SSR；
2. 利用 CookieJar 自动保存首请求下发的 ttwid Cookie 并重试获取完整页面；
3. 从 window._ROUTER_DATA 提取结构化 JSON（辅以正则兜底）；
4. 将 playwm 替换为 play 获取无水印视频、原图列表和背景音乐地址。
"""

from __future__ import annotations

import argparse
import gzip
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1"
)
REFERER = "https://www.douyin.com/"
SHARE_VIDEO_URL_TMPL = "https://www.iesdouyin.com/share/video/{item_id}/"
SHARE_NOTE_URL_TMPL = "https://www.iesdouyin.com/share/note/{item_id}/"
DEFAULT_ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
DEFAULT_ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "dy-dl" / "config.json"

_CONTENT_MARKERS = ("videoInfoRes", "item_list", "playwm", "play_addr")
_TRAILING_URL_PUNCT = "。，；！？、：.,;)]}）】》\"'”’"
_IMAGE_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_AUDIO_MIME_TO_EXT = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
}


def _safe_is_file(candidate: str) -> bool:
    """Safely test whether an argument string is an existing file path."""
    if not candidate or "\x00" in candidate:
        return False
    try:
        return Path(candidate).is_file()
    except (OSError, ValueError):
        return False


def get_input_text(inputs: list[str]) -> str:
    """Read share text from CLI arguments, Termux temp file, clipboard, or stdin."""
    if len(inputs) == 1 and _safe_is_file(inputs[0]):
        return Path(inputs[0]).read_text(encoding="utf-8", errors="replace").strip()

    if inputs:
        return " ".join(inputs).strip()

    if shutil.which("termux-clipboard-get"):
        try:
            proc = subprocess.run(
                ["termux-clipboard-get"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

    if not sys.stdin.isatty():
        return sys.stdin.read().strip()

    return ""


def extract_url_from_text(raw: str) -> str:
    """Extract the first HTTP(S) URL from mixed Douyin share copywriting."""
    match = re.search(r'https?://[^\s"<>]+', raw or "")
    if not match:
        raise ValueError("未检测到有效链接，请传入抖音分享口令/URL 或先复制链接")
    url = match.group(0).rstrip(_TRAILING_URL_PUNCT)
    if not url.startswith(("http://", "https://")):
        raise ValueError("未检测到有效 HTTP(S) 链接")
    return url


def extract_ids(url: str) -> tuple[str, str]:
    """Identify content type ('video' or 'note') and numeric item ID from URL."""
    parsed = urllib.parse.urlparse(url)
    if "/user/" in parsed.path:
        raise ValueError(
            "检测到博主主页链接（/user/）。本脚本免 Cookie 仅支持单条视频、图文和音频下载；"
            "批量抓取主页或收藏夹请使用需要登录 Cookie 的工具（如 f2 或 TikTokDownloader）。"
        )

    patterns = (
        (r"/(?:share/)?video/(\d+)", "video"),
        (r"/(?:share/)?(?:note|slides)/(\d+)", "note"),
        (r"[?&]item_ids=(\d+)", "video"),
        (r"[?&]modal_id=(\d+)", "video"),
    )
    for pattern, kind in patterns:
        match = re.search(pattern, url)
        if match:
            return kind, match.group(1)

    raise ValueError(f"无法从链接中提取作品 ID：{url}")


def decode_unicode_escapes(text: str) -> str:
    """Decode \\uXXXX escapes cleanly without breaking non-ASCII or malformed escapes."""
    def _replace(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    return re.sub(r"\\u([0-9a-fA-F]{4})", _replace, text)


def _find_matching_brace(text: str, start: int) -> int:
    """Find closing '}' matching '{' at start, respecting JSON string literals."""
    depth = 0
    in_string = False
    quote_char = ""
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote_char = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
    raise ValueError("无法解析页面数据：window._ROUTER_DATA 大括号未闭合")


def extract_router_data(html: str) -> tuple[dict[str, Any] | None, str]:
    """Extract window._ROUTER_DATA as (parsed_dict_or_None, decoded_raw_text)."""
    marker = "window._ROUTER_DATA"
    marker_idx = html.find(marker)
    if marker_idx < 0:
        raise ValueError("无法解析页面数据：页面中未找到 window._ROUTER_DATA")

    eq_idx = html.find("=", marker_idx)
    if eq_idx < 0:
        raise ValueError("无法解析页面数据：window._ROUTER_DATA 缺少赋值符号")

    brace_start = html.find("{", eq_idx)
    if brace_start < 0:
        raise ValueError("无法解析页面数据：window._ROUTER_DATA 缺少 JSON 对象")

    decoder = json.JSONDecoder()
    try:
        obj, end_idx = decoder.raw_decode(html, brace_start)
        raw_json = html[brace_start:end_idx]
        decoded_text = decode_unicode_escapes(raw_json)
        return (obj if isinstance(obj, dict) else None), decoded_text
    except json.JSONDecodeError:
        end_idx = _find_matching_brace(html, brace_start)
        raw_json = html[brace_start : end_idx + 1]
        decoded_text = decode_unicode_escapes(raw_json)
        try:
            obj = json.loads(raw_json)
            return (obj if isinstance(obj, dict) else None), decoded_text
        except json.JSONDecodeError:
            return None, decoded_text


def find_item_dict(node: Any) -> dict[str, Any] | None:
    """Recursively find the first item_list[0] dict inside parsed _ROUTER_DATA."""
    if isinstance(node, dict):
        item_list = node.get("item_list")
        if isinstance(item_list, list) and item_list and isinstance(item_list[0], dict):
            return item_list[0]
        for value in node.values():
            found = find_item_dict(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for entry in node:
            found = find_item_dict(entry)
            if found is not None:
                return found
    return None


def _first_http_url(urls: Any) -> str:
    if isinstance(urls, list):
        for url in urls:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
    return ""


def _extract_audio_url_from_node(node: Any) -> str:
    """Extract an HTTP audio URL from a dict, list, or raw URL string."""
    if isinstance(node, str) and node.startswith(("http://", "https://")):
        return node
    if isinstance(node, list):
        return _first_http_url(node)
    if isinstance(node, dict):
        url = _first_http_url(node.get("url_list"))
        if url:
            return url
        uri = node.get("uri")
        if isinstance(uri, str) and uri.startswith(("http://", "https://")):
            return uri
    return ""


def _extract_regex_field(data: str, field: str) -> str:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"(.*?)"', data)
    if not match:
        return ""
    return decode_unicode_escapes(match.group(1))


def _extract_images_regex(router_text: str) -> list[str]:
    match = re.search(r'"images"\s*:\s*\[\s*\{', router_text)
    if not match:
        return []
    bracket_start = router_text.find("[", match.start())
    if bracket_start < 0:
        return []

    depth = 0
    in_string = False
    escaped = False
    limit = min(bracket_start + 300_000, len(router_text))
    for idx in range(bracket_start, limit):
        ch = router_text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                images_block = router_text[bracket_start : idx + 1]
                return re.findall(r'"url_list"\s*:\s*\[\s*"(https?://[^"]+)"', images_block)
    return []


def find_douyinvod_url(text: str) -> str:
    """Fallback scan for direct douyinvod video stream URLs."""
    match = re.search(r'https?://[^"\s<>]*douyinvod[^"\s<>]*', text)
    return match.group(0) if match else ""


def parse_content(
    router_dict: dict[str, Any] | None,
    router_text: str,
    content_type: str,
    item_id: str,
) -> dict[str, Any]:
    """Parse normalized video/gallery/animated metadata from _ROUTER_DATA."""
    item = find_item_dict(router_dict)
    title = ""
    author = ""
    music_url = ""
    images: list[str] = []
    video_url = ""
    animated_video_url = ""

    if item is not None:
        raw_desc = item.get("desc")
        if isinstance(raw_desc, str):
            title = raw_desc

        author_obj = item.get("author")
        if isinstance(author_obj, dict):
            raw_nickname = author_obj.get("nickname")
            if isinstance(raw_nickname, str):
                author = raw_nickname

        music_obj = item.get("music")
        if isinstance(music_obj, dict):
            for key in ("play_url", "audio_track"):
                music_url = _extract_audio_url_from_node(music_obj.get(key))
                if music_url:
                    break
            if not music_url:
                music_url = _extract_audio_url_from_node(music_obj)

        raw_images = item.get("images")
        if isinstance(raw_images, list):
            for img in raw_images:
                if not isinstance(img, dict):
                    continue
                img_url = _first_http_url(img.get("url_list"))
                if img_url:
                    images.append(img_url)
            if len(images) == 1 and isinstance(raw_images[0], dict):
                img_video = raw_images[0].get("video")
                if isinstance(img_video, dict):
                    play_addr = img_video.get("play_addr")
                    if isinstance(play_addr, dict):
                        candidate = _first_http_url(play_addr.get("url_list"))
                        if candidate:
                            animated_video_url = candidate.replace("playwm", "play")

        video_obj = item.get("video")
        if isinstance(video_obj, dict):
            play_addr = video_obj.get("play_addr")
            if isinstance(play_addr, dict):
                candidate = _first_http_url(play_addr.get("url_list"))
                if candidate:
                    video_url = candidate.replace("playwm", "play")

    if not title:
        title = _extract_regex_field(router_text, "desc")
    if not author:
        author = _extract_regex_field(router_text, "nickname")

    if not music_url:
        audio_match = re.search(
            r'"uri"\s*:\s*"(https?://[^"]*\.(?:mp3|m4a|aac)[^"]*)"', router_text
        )
        if not audio_match:
            audio_match = re.search(
                r'"url_list"\s*:\s*\[\s*"(https?://[^"]*\.(?:mp3|m4a|aac)[^"]*)"',
                router_text,
            )
        if audio_match:
            music_url = audio_match.group(1)

    if not images:
        images = _extract_images_regex(router_text)

    if not video_url:
        play_match = re.search(r'"(https?://[^"]*playwm[^"]*)"', router_text)
        if play_match:
            video_url = play_match.group(1).replace("playwm", "play")

    if len(images) == 1 and not animated_video_url:
        animated_video_url = find_douyinvod_url(router_text).replace("playwm", "play")

    if len(images) == 1 and animated_video_url:
        kind = "animated"
        final_video_url = animated_video_url
    elif images and (content_type == "note" or not video_url):
        kind = "gallery"
        final_video_url = ""
    elif video_url:
        kind = "video"
        final_video_url = video_url
    elif images:
        kind = "gallery"
        final_video_url = ""
    else:
        raise ValueError("未能从页面数据中解析出视频或图集地址")

    return {
        "id": item_id,
        "content_type": content_type,
        "kind": kind,
        "title": title,
        "author": author,
        "video_url": final_video_url,
        "images": images,
        "music_url": music_url,
    }


def sanitize_filename(title: str, item_id: str, max_chars: int = 36) -> str:
    """Create a filesystem-safe stem using truncated title and item ID."""
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', " ", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].strip(" ._")
    if not cleaned:
        return f"douyin_{item_id}"
    return f"{cleaned}_{item_id}"


def infer_image_ext_from_url(url: str) -> str:
    """Guess image extension from URL path before checking response Content-Type."""
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith((".jpg", ".jpeg")):
        return ".jpg"
    if path.endswith(".png"):
        return ".png"
    if path.endswith(".gif"):
        return ".gif"
    if path.endswith(".webp"):
        return ".webp"
    return ".jpg"


def infer_audio_ext_from_url(url: str) -> str:
    """Guess audio extension from URL path."""
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(".m4a"):
        return ".m4a"
    if path.endswith(".aac"):
        return ".aac"
    if path.endswith(".wav"):
        return ".wav"
    return ".mp3"


def resolve_output_dirs(output_arg: str | None) -> dict[str, Path]:
    """Choose Android shared media directories on Termux or local ./DouyinDownloads."""
    if output_arg:
        base = Path(output_arg).expanduser()
        return {"video": base, "image": base, "audio": base, "subtitle": base}

    android_root = Path("/storage/emulated/0")
    if android_root.is_dir() and os.access(android_root, os.W_OK):
        return {
            "video": android_root / "Movies" / "Douyin",
            "image": android_root / "Pictures" / "Douyin",
            "audio": android_root / "Music" / "Douyin",
            "subtitle": android_root / "Documents" / "Douyin",
        }

    base = Path.cwd() / "DouyinDownloads"
    return {"video": base, "image": base, "audio": base, "subtitle": base}


def load_config(config_path: Path | None = None) -> dict[str, str]:
    """Load persisted JSON configuration if present."""
    target = config_path or DEFAULT_CONFIG_PATH
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if v is not None}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_config(updates: dict[str, str], config_path: Path | None = None) -> Path:
    """Persist configuration updates atomically with restricted permissions."""
    target = config_path or DEFAULT_CONFIG_PATH
    existing = load_config(target)
    for k, v in updates.items():
        if v:
            existing[k] = v
    write_text_atomic(target, json.dumps(existing, ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def resolve_asr_settings(
    api_key: str | None = None,
    asr_url: str | None = None,
    asr_model: str | None = None,
    config_path: Path | None = None,
) -> dict[str, str]:
    """Resolve ASR credentials and endpoint from CLI args, env vars, saved config, or defaults."""
    cfg = load_config(config_path)
    final_key = (
        api_key
        or os.environ.get("SILICONFLOW_API_KEY")
        or os.environ.get("DY_ASR_API_KEY")
        or cfg.get("api_key")
        or ""
    ).strip()
    final_url = (
        asr_url
        or os.environ.get("DY_ASR_URL")
        or cfg.get("asr_url")
        or DEFAULT_ASR_URL
    ).strip()
    final_model = (
        asr_model
        or os.environ.get("DY_ASR_MODEL")
        or cfg.get("asr_model")
        or DEFAULT_ASR_MODEL
    ).strip()
    return {
        "api_key": final_key,
        "asr_url": final_url,
        "asr_model": final_model,
    }


class DouyinClient:
    """Cookie-aware stdlib HTTP client mirroring QingZai's SSR fetch strategy."""

    def __init__(
        self,
        share_video_tmpl: str = SHARE_VIDEO_URL_TMPL,
        share_note_tmpl: str = SHARE_NOTE_URL_TMPL,
        timeout: int = 30,
    ) -> None:
        self.share_video_tmpl = share_video_tmpl
        self.share_note_tmpl = share_note_tmpl
        self.timeout = timeout
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    def resolve_share_url(self, url: str) -> str:
        """Follow short-link redirects via HEAD, falling back to GET on HTTP 403/405."""
        head_req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT},
            method="HEAD",
        )
        try:
            with self.opener.open(head_req, timeout=self.timeout) as resp:
                return resp.geturl()
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 405):
                raise

        get_req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT},
            method="GET",
        )
        with self.opener.open(get_req, timeout=self.timeout) as resp:
            return resp.geturl()

    @staticmethod
    def has_content(html: str) -> bool:
        return any(marker in html for marker in _CONTENT_MARKERS)

    def _do_fetch_page(self, url: str) -> str:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": REFERER,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "gzip",
            },
        )
        with self.opener.open(req, timeout=self.timeout) as resp:
            raw = resp.read()
            encoding = resp.headers.get("Content-Encoding", "").lower()
            if "gzip" in encoding:
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="replace")

    def fetch_page(self, url: str) -> str:
        """Fetch SSR page with up to 3 attempts so initial ttwid Set-Cookie takes effect."""
        last_html = ""
        for _ in range(3):
            last_html = self._do_fetch_page(url)
            if self.has_content(last_html):
                return last_html
        raise RuntimeError(
            "页面未返回有效视频/图文数据（可能链接已失效，或抖音临时限制了当前网络）"
        )

    def fetch_share_html(self, content_type: str, item_id: str) -> str:
        tmpl = self.share_note_tmpl if content_type == "note" else self.share_video_tmpl
        share_url = tmpl.format(item_id=item_id)
        return self.fetch_page(share_url)

    def download_file(
        self,
        url: str,
        dest: Path,
        infer_image_ext: bool = False,
        infer_audio_ext: bool = False,
        show_progress: bool = True,
    ) -> Path:
        """Stream URL to dest atomically via .part file with 3 retries on transient errors."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        last_err: Exception | None = None

        for attempt in range(3):
            part_path: Path | None = None
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": USER_AGENT, "Referer": REFERER},
                )
                with self.opener.open(req, timeout=max(self.timeout, 60)) as resp:
                    final_dest = dest
                    mime = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    if infer_image_ext and mime in _IMAGE_MIME_TO_EXT:
                        final_dest = dest.with_suffix(_IMAGE_MIME_TO_EXT[mime])
                    elif infer_audio_ext and mime in _AUDIO_MIME_TO_EXT:
                        final_dest = dest.with_suffix(_AUDIO_MIME_TO_EXT[mime])

                    part_path = final_dest.with_name(final_dest.name + ".part")
                    total_header = resp.headers.get("Content-Length")
                    total_bytes = int(total_header) if total_header and total_header.isdigit() else None
                    downloaded = 0

                    with open(part_path, "wb") as fp:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            fp.write(chunk)
                            downloaded += len(chunk)
                            if show_progress:
                                _print_progress(final_dest.name, downloaded, total_bytes)

                if show_progress:
                    sys.stdout.write("\n")
                    sys.stdout.flush()

                os.replace(part_path, final_dest)
                return final_dest
            except urllib.error.HTTPError as exc:
                _cleanup_file(part_path)
                if 400 <= exc.code < 500:
                    raise
                last_err = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                _cleanup_file(part_path)
                last_err = exc

            if attempt < 2:
                time.sleep(attempt + 1)

        raise RuntimeError(f"下载失败 ({dest.name}): {last_err}") from last_err


def _cleanup_file(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _print_progress(filename: str, downloaded: int, total: int | None) -> None:
    mb_done = downloaded / (1024 * 1024)
    if total and total > 0:
        pct = min(100, int(downloaded * 100 / total))
        mb_total = total / (1024 * 1024)
        line = f"\r下载 {filename}: {pct:3d}% ({mb_done:.1f}/{mb_total:.1f} MB)"
    else:
        line = f"\r下载 {filename}: {mb_done:.1f} MB"
    sys.stdout.write(line)
    sys.stdout.flush()


def _atomic_copy(src: Path, dest: Path) -> None:
    """Copy a completed temp file to dest atomically via a .part staging file."""
    part_dest = dest.with_name(dest.name + ".part")
    try:
        shutil.copyfile(src, part_dest)
        os.replace(part_dest, dest)
    except Exception:
        _cleanup_file(part_dest)
        raise


def extract_audio_from_video(video_path: Path, dest_mp3: Path) -> Path:
    """Extract audio track from a video file using ffmpeg (MP3 first, lossless M4A fallback)."""
    dest_mp3.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dy_audio_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        tmp_mp3 = tmp_dir / "audio.mp3"
        proc_mp3 = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(tmp_mp3),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_mp3.returncode == 0 and tmp_mp3.is_file() and tmp_mp3.stat().st_size > 0:
            final_dest = dest_mp3.with_suffix(".mp3")
            _atomic_copy(tmp_mp3, final_dest)
            return final_dest

        tmp_m4a = tmp_dir / "audio.m4a"
        proc_m4a = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-c:a",
                "copy",
                str(tmp_m4a),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_m4a.returncode == 0 and tmp_m4a.is_file() and tmp_m4a.stat().st_size > 0:
            final_dest = dest_mp3.with_suffix(".m4a")
            _atomic_copy(tmp_m4a, final_dest)
            return final_dest

        tail = (
            proc_m4a.stderr
            or proc_m4a.stdout
            or proc_mp3.stderr
            or proc_mp3.stdout
            or ""
        ).strip()[-400:]
        raise RuntimeError(f"ffmpeg 提取音频失败: {tail}")


def download_and_extract_audio(
    client: DouyinClient,
    video_url: str,
    dest_mp3: Path,
    show_progress: bool = True,
) -> Path:
    """Download video to a temporary directory, extract audio, and discard temp video."""
    with tempfile.TemporaryDirectory(prefix="dy_video_tmp_") as tmp_dir_str:
        video_tmp = client.download_file(
            video_url, Path(tmp_dir_str) / "video.mp4", show_progress=show_progress
        )
        return extract_audio_from_video(video_tmp, dest_mp3)


def merge_video_and_audio(
    client: DouyinClient,
    video_url: str,
    music_url: str,
    dest: Path,
    show_progress: bool = True,
) -> Path:
    """Download animated video + background audio to temp dir and mux with ffmpeg."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dy_merge_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        video_tmp = client.download_file(
            video_url, tmp_dir / "video.mp4", show_progress=show_progress
        )
        audio_ext = infer_audio_ext_from_url(music_url)
        audio_tmp = client.download_file(
            music_url,
            tmp_dir / f"music{audio_ext}",
            infer_audio_ext=True,
            show_progress=show_progress,
        )
        out_tmp = tmp_dir / "merged.mp4"

        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_tmp),
                "-i",
                str(audio_tmp),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-shortest",
                str(out_tmp),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not out_tmp.is_file():
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise RuntimeError(f"ffmpeg 合成失败: {tail}")

        _atomic_copy(out_tmp, dest)
    return dest


def write_text_atomic(dest: Path, content: str) -> Path:
    """Write UTF-8 text atomically to dest via a .part staging file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest.with_name(dest.name + ".part")
    try:
        part_path.write_text(content, encoding="utf-8")
        os.replace(part_path, dest)
    except Exception:
        _cleanup_file(part_path)
        raise
    return dest


def copy_to_clipboard(text: str) -> bool:
    """Copy text to Android clipboard via termux-clipboard-set if available."""
    if text and shutil.which("termux-clipboard-set"):
        try:
            proc = subprocess.run(
                ["termux-clipboard-set"],
                input=text,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    return False


def prepare_audio_for_asr(media_path: Path, tmp_dir: Path) -> Path:
    """Transcode media into compact 16kHz mono MP3 (or WAV fallback) for ASR upload."""
    if shutil.which("ffmpeg"):
        mp3_out = tmp_dir / "asr.mp3"
        proc_mp3 = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(media_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "64k",
                str(mp3_out),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_mp3.returncode == 0 and mp3_out.is_file() and mp3_out.stat().st_size > 0:
            return mp3_out

        wav_out = tmp_dir / "asr.wav"
        proc_wav = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(media_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav_out),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_wav.returncode == 0 and wav_out.is_file() and wav_out.stat().st_size > 0:
            return wav_out

        tail = (
            proc_wav.stderr
            or proc_wav.stdout
            or proc_mp3.stderr
            or proc_mp3.stdout
            or ""
        ).strip()[-400:]
        raise RuntimeError(f"ffmpeg 预处理转录音频失败: {tail}")

    if media_path.suffix.lower() in (".mp3", ".wav", ".m4a", ".flac", ".ogg"):
        return media_path

    raise RuntimeError("从视频提取音频用于转录需要 ffmpeg，请在 Termux 执行 pkg install -y ffmpeg")


def probe_audio_duration(audio_path: Path) -> float | None:
    """Return media duration in seconds using ffprobe if installed."""
    if not shutil.which("ffprobe"):
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            val = float(proc.stdout.strip())
            if val > 0:
                return val
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def build_multipart_body(
    fields: list[tuple[str, str]],
    file_field: str,
    filename: str,
    file_bytes: bytes,
    content_type: str,
) -> tuple[bytes, str]:
    """Construct an RFC 7578 multipart/form-data request body using stdlib only."""
    boundary = f"----dyDlBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    safe_name = filename.replace('"', "_")
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(file_bytes)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def transcribe_audio_api(
    audio_path: Path,
    api_key: str,
    api_url: str = DEFAULT_ASR_URL,
    model: str = DEFAULT_ASR_MODEL,
    timeout: int = 120,
) -> dict[str, Any]:
    """Upload audio to SiliconFlow / OpenAI-compatible transcription endpoint."""
    key = (api_key or "").strip()
    if not key:
        raise ValueError(
            '未配置语音转录 API Key。请先运行 dy --set-key "sk-你的硅基流动Key"，'
            "或设置环境变量 SILICONFLOW_API_KEY。"
        )

    ext_to_mime = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
    }
    mime = ext_to_mime.get(audio_path.suffix.lower(), "application/octet-stream")
    fields: list[tuple[str, str]] = [("model", model)]
    if "siliconflow" not in api_url.lower():
        fields.append(("response_format", "verbose_json"))

    body, boundary = build_multipart_body(
        fields=fields,
        file_field="file",
        filename=audio_path.name,
        file_bytes=audio_path.read_bytes(),
        content_type=mime,
    )
    req = urllib.request.Request(
        api_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_resp = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()[:400]
        except Exception:
            pass
        raise RuntimeError(f"转录接口报错 HTTP {exc.code}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"转录请求失败: {exc}") from exc

    try:
        data = json.loads(raw_resp.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"转录接口返回非 JSON 内容: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("转录接口返回格式异常")

    segments = data.get("segments")
    return {
        "text": str(data.get("text") or ""),
        "segments": segments if isinstance(segments, list) else [],
    }


def clean_asr_text(text: str) -> str:
    """Strip SenseVoice inline tags like <|zh|><|NEUTRAL|> and normalize spacing."""
    cleaned = re.sub(r"<\|[^|>]+\|>", "", text or "")
    cleaned = re.sub(r"[ \t\r\f\v]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()


def split_sentences_for_subtitles(text: str, max_line_chars: int = 28) -> list[str]:
    """Split transcript text into readable sentence/clause lines for .txt and .srt."""
    normalized = clean_asr_text(text)
    if not normalized:
        return []

    raw_sentences: list[str] = []
    parts = re.split(r"([。！？!?；;\n]+)", normalized)
    buf = ""
    for part in parts:
        if not part:
            continue
        if re.fullmatch(r"[。！？!?；;\n]+", part):
            punct = part.replace("\n", "").strip()
            candidate = (buf + punct).strip()
            if candidate:
                raw_sentences.append(candidate)
            buf = ""
        else:
            buf += part
    if buf.strip():
        raw_sentences.append(buf.strip())

    lines: list[str] = []
    for sentence in raw_sentences:
        if len(sentence) <= max_line_chars:
            lines.append(sentence)
            continue
        clauses = re.split(r"([，,、]+)", sentence)
        current = ""
        i = 0
        while i < len(clauses):
            piece = clauses[i]
            if i + 1 < len(clauses) and re.fullmatch(r"[，,、]+", clauses[i + 1]):
                piece += clauses[i + 1]
                i += 2
            else:
                i += 1
            piece = piece.strip()
            if not piece:
                continue
            if current and len(current) + len(piece) > max_line_chars:
                lines.append(current)
                current = piece
            else:
                current = f"{current}{piece}" if current else piece
        if current:
            lines.append(current)

    return [line for line in lines if line] or [normalized]


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds into SRT timestamp HH:MM:SS,mmm."""
    ms_total = max(0, round(seconds * 1000))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt_and_txt(
    text: str,
    segments: list[dict[str, Any]] | None,
    duration_sec: float | None,
) -> tuple[str, str, bool]:
    """Build (.srt, .txt, is_estimated_timing) from exact segments or plain text."""
    cleaned_segments: list[tuple[float, float, str]] = []
    if segments:
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            try:
                start = float(seg["start"])
                end = float(seg["end"])
            except (KeyError, TypeError, ValueError):
                continue
            seg_text = clean_asr_text(str(seg.get("text") or ""))
            if seg_text and end >= start:
                cleaned_segments.append((start, end, seg_text))

    if cleaned_segments:
        srt_blocks = [
            f"{idx}\n{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n{seg_text}"
            for idx, (start, end, seg_text) in enumerate(cleaned_segments, start=1)
        ]
        txt_content = "\n".join(seg_text for _, _, seg_text in cleaned_segments) + "\n"
        return "\n\n".join(srt_blocks) + "\n", txt_content, False

    cleaned = clean_asr_text(text)
    if not cleaned:
        raise RuntimeError("转录结果为空，可能视频中没有清晰人声")

    sentences = split_sentences_for_subtitles(cleaned)
    txt_content = "\n".join(sentences) + "\n"
    total_dur = duration_sec if (duration_sec and duration_sec > 0) else max(2.0, len(cleaned) / 4.5)
    weights = [max(1, len(sentence)) for sentence in sentences]
    weight_sum = sum(weights)

    srt_blocks = []
    cursor = 0.0
    for idx, (sentence, weight) in enumerate(zip(sentences, weights), start=1):
        end = total_dur if idx == len(sentences) else cursor + total_dur * (weight / weight_sum)
        if end <= cursor:
            end = cursor + 0.1
        srt_blocks.append(
            f"{idx}\n{format_srt_timestamp(cursor)} --> {format_srt_timestamp(end)}\n{sentence}"
        )
        cursor = end

    return "\n\n".join(srt_blocks) + "\n", txt_content, True


def _transcribe_media_to_dir(
    media_path: Path,
    out_dir: Path,
    stem: str,
    settings: dict[str, str],
) -> tuple[list[Path], str, bool]:
    """Prepare audio, call ASR API, and write matching .txt and .srt files."""
    with tempfile.TemporaryDirectory(prefix="dy_asr_") as tmp_dir_str:
        asr_audio = prepare_audio_for_asr(media_path, Path(tmp_dir_str))
        duration = probe_audio_duration(asr_audio) or probe_audio_duration(media_path)
        asr_res = transcribe_audio_api(
            asr_audio,
            api_key=settings["api_key"],
            api_url=settings["asr_url"],
            model=settings["asr_model"],
        )
    srt_content, txt_content, is_est = build_srt_and_txt(
        asr_res["text"],
        asr_res["segments"],
        duration,
    )
    txt_path = write_text_atomic(out_dir / f"{stem}.txt", txt_content)
    srt_path = write_text_atomic(out_dir / f"{stem}.srt", srt_content)
    return [txt_path, srt_path], txt_content, is_est


def notify_termux(saved_files: list[Path], summary: str) -> None:
    """Refresh Android MediaStore via termux-media-scan and show optional toast."""
    if saved_files and shutil.which("termux-media-scan"):
        try:
            subprocess.run(
                ["termux-media-scan", *[str(p) for p in saved_files]],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    if shutil.which("termux-toast"):
        try:
            subprocess.run(
                ["termux-toast", summary],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def process_url(
    raw_input: str,
    client: DouyinClient,
    dirs: dict[str, Path],
    audio_only: bool = False,
    with_audio: bool = False,
    no_merge: bool = False,
    show_progress: bool = True,
    transcribe: bool = False,
    subtitle_only: bool = False,
    asr_settings: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[Path]]:
    """End-to-end parse and download pipeline for a single share input."""
    settings: dict[str, str] | None = None
    if transcribe or subtitle_only:
        settings = asr_settings if asr_settings is not None else resolve_asr_settings()
        if not settings.get("api_key", "").strip():
            raise ValueError(
                '未配置语音转录 API Key。请先运行 dy --set-key "sk-你的硅基流动Key"，'
                "或设置环境变量 SILICONFLOW_API_KEY。"
            )

    extracted_url = extract_url_from_text(raw_input)
    resolved_url = client.resolve_share_url(extracted_url)
    content_type, item_id = extract_ids(resolved_url)

    html = client.fetch_share_html(content_type, item_id)
    router_dict, router_text = extract_router_data(html)
    info = parse_content(router_dict, router_text, content_type, item_id)
    stem = sanitize_filename(info["title"], info["id"])

    saved_files: list[Path] = []

    if subtitle_only:
        assert settings is not None
        media_url = info["video_url"] or info["music_url"]
        if not media_url:
            raise RuntimeError("该作品没有可供转录的视频或音频流")
        media_ext = ".mp4" if info["video_url"] else infer_audio_ext_from_url(media_url)
        with tempfile.TemporaryDirectory(prefix="dy_sub_tmp_") as tmp_dir_str:
            media_tmp = client.download_file(
                media_url,
                Path(tmp_dir_str) / f"media{media_ext}",
                infer_audio_ext=not bool(info["video_url"]),
                show_progress=show_progress,
            )
            print("🎙️ 正在提取音频并调用语音转录接口...")
            sub_files, txt_content, is_est = _transcribe_media_to_dir(
                media_tmp, dirs["subtitle"], stem, settings
            )
        saved_files.extend(sub_files)
        info["transcript_text"] = txt_content
        info["srt_estimated"] = is_est
        return info, saved_files

    if audio_only:
        if info["music_url"]:
            audio_dest = dirs["audio"] / f"{stem}{infer_audio_ext_from_url(info['music_url'])}"
            saved_files.append(
                client.download_file(
                    info["music_url"],
                    audio_dest,
                    infer_audio_ext=True,
                    show_progress=show_progress,
                )
            )
        elif info["video_url"]:
            if shutil.which("ffmpeg"):
                print("ℹ️ 分享页未单独提供音频直链，正在通过 ffmpeg 从视频提取音频...")
                saved_files.append(
                    download_and_extract_audio(
                        client,
                        info["video_url"],
                        dirs["audio"] / f"{stem}.mp3",
                        show_progress=show_progress,
                    )
                )
            else:
                raise RuntimeError(
                    "该视频分享页未单独提供音频直链（声音内嵌在视频内）。"
                    "请在 Termux 执行 pkg install -y ffmpeg 后重试 dya，即可自动从视频提取音频。"
                )
        else:
            raise RuntimeError("该作品未提供可下载的背景音乐或视频音频流")
        if transcribe:
            assert settings is not None
            print("🎙️ 正在调用语音转录接口...")
            sub_files, txt_content, is_est = _transcribe_media_to_dir(
                saved_files[0], dirs["subtitle"], stem, settings
            )
            saved_files.extend(sub_files)
            info["transcript_text"] = txt_content
            info["srt_estimated"] = is_est
        return info, saved_files

    kind = info["kind"]
    if kind == "video":
        video_dest = dirs["video"] / f"{stem}.mp4"
        saved_video = client.download_file(info["video_url"], video_dest, show_progress=show_progress)
        saved_files.append(saved_video)
        if with_audio:
            if info["music_url"]:
                audio_dest = dirs["audio"] / f"{stem}{infer_audio_ext_from_url(info['music_url'])}"
                saved_files.append(
                    client.download_file(
                        info["music_url"],
                        audio_dest,
                        infer_audio_ext=True,
                        show_progress=show_progress,
                    )
                )
            elif shutil.which("ffmpeg"):
                print("ℹ️ 分享页未单独提供音频直链，正在通过 ffmpeg 从已下载视频提取音频...")
                saved_files.append(
                    extract_audio_from_video(saved_video, dirs["audio"] / f"{stem}.mp3")
                )
            else:
                print(
                    "⚠️ 该视频分享页未单独提供音频直链，需执行 pkg install -y ffmpeg 才能自动从视频提取音频。",
                    file=sys.stderr,
                )
    elif kind == "gallery":
        images: list[str] = info["images"]
        for idx, img_url in enumerate(images, start=1):
            suffix = f"_{idx:02d}" if len(images) > 1 else ""
            ext_guess = infer_image_ext_from_url(img_url)
            img_dest = dirs["image"] / f"{stem}{suffix}{ext_guess}"
            saved_files.append(
                client.download_file(
                    img_url,
                    img_dest,
                    infer_image_ext=True,
                    show_progress=show_progress,
                )
            )
        if with_audio and info["music_url"]:
            audio_dest = dirs["audio"] / f"{stem}{infer_audio_ext_from_url(info['music_url'])}"
            saved_files.append(
                client.download_file(
                    info["music_url"],
                    audio_dest,
                    infer_audio_ext=True,
                    show_progress=show_progress,
                )
            )
    elif kind == "animated":
        video_dest = dirs["video"] / f"{stem}.mp4"
        merged = False
        if info["music_url"] and not no_merge and shutil.which("ffmpeg"):
            try:
                saved_files.append(
                    merge_video_and_audio(
                        client,
                        info["video_url"],
                        info["music_url"],
                        video_dest,
                        show_progress=show_progress,
                    )
                )
                merged = True
            except Exception as exc:
                print(f"⚠️ 动图音频合成失败，回退为分别保存视频与音频: {exc}", file=sys.stderr)

        if not merged:
            saved_files.append(
                client.download_file(info["video_url"], video_dest, show_progress=show_progress)
            )

        if info["music_url"] and (not merged or with_audio):
            audio_dest = dirs["audio"] / f"{stem}{infer_audio_ext_from_url(info['music_url'])}"
            saved_files.append(
                client.download_file(
                    info["music_url"],
                    audio_dest,
                    infer_audio_ext=True,
                    show_progress=show_progress,
                )
            )
    else:
        raise RuntimeError(f"未知内容类型: {kind}")

    if transcribe:
        assert settings is not None
        print("🎙️ 正在提取音频并调用语音转录接口...")
        if kind in ("video", "animated"):
            sub_files, txt_content, is_est = _transcribe_media_to_dir(
                saved_files[0], dirs["video"], stem, settings
            )
        elif with_audio and info["music_url"] and saved_files:
            sub_files, txt_content, is_est = _transcribe_media_to_dir(
                saved_files[-1], dirs["subtitle"], stem, settings
            )
        elif info["music_url"]:
            with tempfile.TemporaryDirectory(prefix="dy_gal_asr_") as tmp_dir_str:
                audio_tmp = client.download_file(
                    info["music_url"],
                    Path(tmp_dir_str) / f"music{infer_audio_ext_from_url(info['music_url'])}",
                    infer_audio_ext=True,
                    show_progress=show_progress,
                )
                sub_files, txt_content, is_est = _transcribe_media_to_dir(
                    audio_tmp, dirs["subtitle"], stem, settings
                )
        else:
            raise RuntimeError("该图集未提供背景音乐，无法生成音频字幕")
        saved_files.extend(sub_files)
        info["transcript_text"] = txt_content
        info["srt_estimated"] = is_est

    return info, saved_files


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="免 Cookie 抖音无水印下载器（支持视频、图文、音频、云端转录字幕）",
    )
    parser.add_argument(
        "input",
        nargs="*",
        help="抖音分享文本/链接，或由 Termux 传入的临时文本文件路径（留空则读取剪贴板）",
    )
    parser.add_argument(
        "-a",
        "--audio-only",
        action="store_true",
        help="仅下载背景音乐 / 音频",
    )
    parser.add_argument(
        "-m",
        "--with-audio",
        action="store_true",
        help="下载视频或图文的同时，额外保存背景音乐",
    )
    parser.add_argument(
        "-s",
        "--subtitle-only",
        action="store_true",
        help="仅提取并转录字幕（生成 .srt 与 .txt，不保留视频文件）",
    )
    parser.add_argument(
        "-t",
        "--transcribe",
        action="store_true",
        help="下载媒体文件的同时生成同名 .srt 字幕与 .txt 逐字稿",
    )
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="转录完成后不自动将逐字稿复制到手机剪贴板",
    )
    parser.add_argument(
        "--set-key",
        default=None,
        metavar="KEY",
        help="保存语音转录 API Key 到本地配置（可配合 --asr-url / --asr-model 使用）",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="临时指定本次语音转录 API Key",
    )
    parser.add_argument(
        "--asr-url",
        default=None,
        help=f"自定义语音转录接口地址（默认 {DEFAULT_ASR_URL}）",
    )
    parser.add_argument(
        "--asr-model",
        default=None,
        help=f"自定义语音转录模型名（默认 {DEFAULT_ASR_MODEL}）",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="自定义保存目录（默认在 Termux 存手机共享目录，电脑存 ./DouyinDownloads）",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="动图作品不调用 ffmpeg 合并背景音乐，分别保存无声视频与音频",
    )
    parser.add_argument(
        "--pause-on-error",
        action="store_true",
        help="发生错误时暂停等待回车，方便在 Termux 快捷方式弹窗中查看报错",
    )
    return parser


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if args.set_key is not None:
            cleaned_key = args.set_key.strip()
            if not cleaned_key:
                raise ValueError("API Key 不能为空")
            updates = {"api_key": cleaned_key}
            if args.asr_url:
                updates["asr_url"] = args.asr_url.strip()
            if args.asr_model:
                updates["asr_model"] = args.asr_model.strip()
            cfg_path = save_config(updates)
            print(f"✅ 已保存转录 API 配置（Key: {_mask_key(cleaned_key)}）")
            print(f"   📁 配置文件：{cfg_path}")
            return 0

        raw_input = get_input_text(args.input)
        if not raw_input:
            raise ValueError("未获取到输入内容，请传入抖音分享链接或先在抖音点「复制链接」")

        dirs = resolve_output_dirs(args.output)
        asr_settings = resolve_asr_settings(
            api_key=args.api_key,
            asr_url=args.asr_url,
            asr_model=args.asr_model,
        )
        client = DouyinClient()
        print("🔍 正在解析抖音分享链接...")
        info, saved_files = process_url(
            raw_input=raw_input,
            client=client,
            dirs=dirs,
            audio_only=args.audio_only,
            with_audio=args.with_audio,
            no_merge=args.no_merge,
            show_progress=sys.stdout.isatty(),
            transcribe=args.transcribe,
            subtitle_only=args.subtitle_only,
            asr_settings=asr_settings,
        )

        kind_label = {
            "video": "无水印视频",
            "gallery": f"图集 ({len(info['images'])} 张)",
            "animated": "实况/动图",
        }.get(info["kind"], info["kind"])
        if args.subtitle_only:
            kind_label = "字幕与逐字稿"
        elif args.audio_only and args.transcribe:
            kind_label = "音频 + 字幕"
        elif args.audio_only:
            kind_label = "音频"
        elif args.transcribe:
            kind_label = f"{kind_label} + 字幕"

        if info["content_type"] == "note" and info["kind"] == "gallery" and len(info["images"]) == 1:
            print("ℹ️ 提示：单图实况若视频流仅由前端 JS 动态生成，CLI 将保存高清原图（可加 -m 同时保存背景音乐）。")

        print(f"✅ 下载完成 [{kind_label}]：{info['title'] or info['id']}")
        for path in saved_files:
            print(f"   📁 {path}")

        transcript_text = str(info.get("transcript_text") or "")
        if transcript_text:
            preview = transcript_text.replace("\n", " ").strip()
            if len(preview) > 300:
                preview = preview[:300] + "..."
            print(f"📝 逐字稿预览：{preview}")
            if info.get("srt_estimated"):
                print("ℹ️ 提示：当前接口仅返回纯文本，.srt 时间轴为按句长估算；.txt 逐字稿可直接阅读或发给 AI 总结。")
            if not args.no_copy and copy_to_clipboard(transcript_text):
                print("📋 逐字稿已自动复制到剪贴板")

        notify_termux(saved_files, f"抖音{kind_label}完成（共 {len(saved_files)} 个文件）")
        return 0
    except Exception as exc:
        msg = str(exc)
        print(f"❌ 失败：{msg}", file=sys.stderr)
        notify_termux([], f"抖音下载失败：{msg[:60]}")
        if args.pause_on_error and sys.stdin.isatty():
            try:
                input("按回车键退出...")
            except EOFError:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
