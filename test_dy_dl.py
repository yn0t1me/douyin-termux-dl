#!/usr/bin/env python3
"""Unit and end-to-end mock-HTTP tests for dy_dl.py."""

from __future__ import annotations

import gzip
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dy_dl


class MockDouyinHandler(http.server.BaseHTTPRequestHandler):
    """Mock HTTP server simulating Douyin redirects, ttwid SSR cookie retry, and media files."""

    state: dict = {}

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _send_bytes(
        self,
        code: int,
        data: bytes,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
        send_body: bool = True,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        if send_body and data:
            self.wfile.write(data)

    def do_HEAD(self) -> None:  # noqa: N802
        base_url = self.state["base_url"]
        if self.path == "/short_ok":
            self.send_response(302)
            self.send_header("Location", f"{base_url}/video/7400000000000000001/")
            self.end_headers()
            return
        if self.path == "/short_animated":
            self.send_response(302)
            self.send_header("Location", f"{base_url}/share/note/7400000000000000003/")
            self.end_headers()
            return
        if self.path == "/short_animated_vod":
            self.send_response(302)
            self.send_header("Location", f"{base_url}/share/note/7400000000000000004/")
            self.end_headers()
            return
        if self.path == "/short_no_music":
            self.send_response(302)
            self.send_header("Location", f"{base_url}/video/7400000000000000005/")
            self.end_headers()
            return
        if self.path == "/short_405":
            self._send_bytes(405, b"", send_body=False)
            return
        if self.path in (
            "/video/7400000000000000001/",
            "/video/7400000000000000005/",
            "/share/note/7400000000000000003/",
            "/share/note/7400000000000000004/",
        ):
            self._send_bytes(200, b"", send_body=False)
            return
        self._send_bytes(404, b"", send_body=False)

    def do_GET(self) -> None:  # noqa: N802
        base_url = self.state["base_url"]

        if self.path == "/short_405":
            self.send_response(302)
            self.send_header("Location", f"{base_url}/note/7400000000000000002/")
            self.end_headers()
            return

        if self.path == "/short_animated":
            self.send_response(302)
            self.send_header("Location", f"{base_url}/share/note/7400000000000000003/")
            self.end_headers()
            return

        if self.path == "/short_animated_vod":
            self.send_response(302)
            self.send_header("Location", f"{base_url}/share/note/7400000000000000004/")
            self.end_headers()
            return

        if self.path in (
            "/video/7400000000000000001/",
            "/note/7400000000000000002/",
            "/video/7400000000000000005/",
        ):
            self._send_bytes(200, b"ok")
            return

        # SSR video page requiring ttwid cookie on second attempt + gzip compression
        if self.path == "/share/video/7400000000000000001/":
            self.state["ssr_video_hits"] = self.state.get("ssr_video_hits", 0) + 1
            cookie = self.headers.get("Cookie", "")
            if "ttwid=mock123" not in cookie:
                shell = "<html><body>empty shell</body></html>".encode("utf-8")
                self._send_bytes(
                    200,
                    shell,
                    headers={"Set-Cookie": "ttwid=mock123; Path=/"},
                )
                return

            payload = {
                "loaderData": {
                    "video_(id)/page": {
                        "videoInfoRes": {
                            "item_list": [
                                {
                                    "aweme_id": "7400000000000000001",
                                    "desc": "测试{花括号}视频\\u4e2d\\u6587",
                                    "author": {"nickname": "测试作者"},
                                    "video": {
                                        "play_addr": {
                                            "url_list": [
                                                f"{base_url}/media/playwm?video_id=abc123"
                                            ]
                                        }
                                    },
                                    "music": {
                                        "play_url": {
                                            "url_list": [f"{base_url}/media/bgm.mp3"]
                                        }
                                    },
                                }
                            ]
                        }
                    }
                }
            }
            # Put an actual brace and unicode escape inside HTML JSON
            json_str = json.dumps(payload, ensure_ascii=True).replace("\\\\u", "\\u")
            html = f"<html><script>window._ROUTER_DATA = {json_str}</script></html>".encode(
                "utf-8"
            )
            gz = gzip.compress(html)
            self._send_bytes(
                200,
                gz,
                headers={"Content-Encoding": "gzip"},
            )
            return

        # SSR gallery note page
        if self.path == "/share/note/7400000000000000002/":
            payload = {
                "loaderData": {
                    "note_(id)/page": {
                        "videoInfoRes": {
                            "item_list": [
                                {
                                    "aweme_id": "7400000000000000002",
                                    "desc": "好看的图集",
                                    "author": {"nickname": "摄影师"},
                                    "images": [
                                        {"url_list": [f"{base_url}/media/img1"]},
                                        {"url_list": [f"{base_url}/media/img2"]},
                                    ],
                                    "music": {
                                        "play_url": {
                                            "url_list": [f"{base_url}/media/note_bgm.mp3"]
                                        }
                                    },
                                }
                            ]
                        }
                    }
                }
            }
            html = (
                f"<html><script>window._ROUTER_DATA = {json.dumps(payload)}</script></html>"
            ).encode("utf-8")
            self._send_bytes(200, html)
            return

        # SSR animated note page (images[0].video)
        if self.path == "/share/note/7400000000000000003/":
            payload = {
                "loaderData": {
                    "note_(id)/page": {
                        "videoInfoRes": {
                            "item_list": [
                                {
                                    "aweme_id": "7400000000000000003",
                                    "desc": "实况动图1",
                                    "author": {"nickname": "动图作者"},
                                    "images": [
                                        {
                                            "url_list": [f"{base_url}/media/img1"],
                                            "video": {
                                                "play_addr": {
                                                    "url_list": [
                                                        f"{base_url}/media/playwm?video_id=live1"
                                                    ]
                                                }
                                            },
                                        }
                                    ],
                                    "music": {"uri": f"{base_url}/media/note_bgm.mp3"},
                                }
                            ]
                        }
                    }
                }
            }
            html = (
                f"<html><script>window._ROUTER_DATA = {json.dumps(payload)}</script></html>"
            ).encode("utf-8")
            self._send_bytes(200, html)
            return

        # SSR animated note page (douyinvod fallback in router text)
        if self.path == "/share/note/7400000000000000004/":
            payload = {
                "loaderData": {
                    "note_(id)/page": {
                        "videoInfoRes": {
                            "item_list": [
                                {
                                    "aweme_id": "7400000000000000004",
                                    "desc": "实况动图2",
                                    "author": {"nickname": "动图作者2"},
                                    "images": [
                                        {"url_list": [f"{base_url}/media/img1"]},
                                    ],
                                    "extra_stream": f"{base_url}/media/douyinvod_live.mp4",
                                }
                            ]
                        }
                    }
                }
            }
            html = (
                f"<html><script>window._ROUTER_DATA = {json.dumps(payload)}</script></html>"
            ).encode("utf-8")
            self._send_bytes(200, html)
            return

        # SSR video page with NO separate music field (real audio embedded in MP4)
        if self.path == "/share/video/7400000000000000005/":
            payload = {
                "loaderData": {
                    "video_(id)/page": {
                        "videoInfoRes": {
                            "item_list": [
                                {
                                    "aweme_id": "7400000000000000005",
                                    "desc": "无独立音频直链视频",
                                    "author": {"nickname": "原声作者"},
                                    "video": {
                                        "play_addr": {
                                            "url_list": [
                                                f"{base_url}/media/playwm?video_id=real_av"
                                            ]
                                        }
                                    },
                                }
                            ]
                        }
                    }
                }
            }
            html = (
                f"<html><script>window._ROUTER_DATA = {json.dumps(payload)}</script></html>"
            ).encode("utf-8")
            self._send_bytes(200, html)
            return

        if self.path == "/media/play?video_id=abc123":
            self._send_bytes(200, b"MOCK_NO_WM_MP4_DATA", content_type="video/mp4")
            return

        if self.path == "/media/play?video_id=real_av":
            self.state["real_av_hits"] = self.state.get("real_av_hits", 0) + 1
            self._send_bytes(200, self.state["sample_mp4"], content_type="video/mp4")
            return

        if self.path == "/media/play?video_id=live1":
            self._send_bytes(200, b"MOCK_ANIMATED_LIVE_MP4", content_type="video/mp4")
            return

        if self.path == "/media/douyinvod_live.mp4":
            self._send_bytes(200, b"MOCK_DOUYINVOD_LIVE_MP4", content_type="video/mp4")
            return

        if self.path == "/media/playwm?video_id=abc123":
            self._send_bytes(403, b"Watermarked endpoint should not be called")
            return

        if self.path in ("/media/bgm.mp3", "/media/note_bgm.mp3"):
            self._send_bytes(200, b"MOCK_MP3_AUDIO", content_type="audio/mpeg")
            return

        if self.path == "/media/img1":
            self._send_bytes(200, b"PNG_BYTES", content_type="image/png")
            return

        if self.path == "/media/img2":
            self._send_bytes(200, b"WEBP_BYTES", content_type="image/webp")
            return

        if self.path == "/media/broken":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"partial")
            return

        self._send_bytes(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        boundary_match = re.search(r"boundary=([^\s;]+)", content_type)
        fields: dict[str, str] = {}
        file_name = ""
        file_len = 0

        if boundary_match:
            boundary = boundary_match.group(1).strip('"').encode("ascii")
            for part in raw_body.split(b"--" + boundary):
                if b"\r\n\r\n" not in part:
                    continue
                header_blob, body_blob = part.split(b"\r\n\r\n", 1)
                if body_blob.endswith(b"\r\n"):
                    body_blob = body_blob[:-2]
                header_text = header_blob.decode("utf-8", errors="replace")
                name_m = re.search(r'name="([^"]+)"', header_text)
                if not name_m:
                    continue
                field_name = name_m.group(1)
                file_m = re.search(r'filename="([^"]+)"', header_text)
                if file_m:
                    file_name = file_m.group(1)
                    file_len = len(body_blob)
                else:
                    fields[field_name] = body_blob.decode("utf-8", errors="replace")

        self.state["last_asr_post"] = {
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "fields": fields,
            "file_name": file_name,
            "file_len": file_len,
        }

        if self.path == "/siliconflow/v1/audio/transcriptions":
            resp_json = json.dumps(
                {"text": "<|zh|><|NEUTRAL|>大家好，今天测试一下手机转录！非常好用。"},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send_bytes(200, resp_json, content_type="application/json; charset=utf-8")
            return

        if self.path == "/openai/v1/audio/transcriptions":
            resp_json = json.dumps(
                {
                    "text": "Hello world. Second line.",
                    "segments": [
                        {"start": 0.0, "end": 1.25, "text": "Hello world."},
                        {"start": 1.25, "end": 2.5, "text": "Second line."},
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self._send_bytes(200, resp_json, content_type="application/json; charset=utf-8")
            return

        self._send_bytes(404, b"not found")


class DouyinDownloaderTests(unittest.TestCase):
    server: http.server.ThreadingHTTPServer
    thread: threading.Thread
    base_url: str
    sample_mp4: bytes = b""

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("ffmpeg"):
            with tempfile.TemporaryDirectory() as tmpdir:
                clip = Path(tmpdir) / "sample.mp4"
                proc = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc=duration=0.2:size=64x64:rate=10",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:duration=0.2",
                        "-c:v",
                        "mpeg4",
                        "-c:a",
                        "aac",
                        str(clip),
                    ],
                    capture_output=True,
                    check=False,
                )
                if proc.returncode == 0 and clip.is_file():
                    cls.sample_mp4 = clip.read_bytes()

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockDouyinHandler)
        port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{port}"
        MockDouyinHandler.state = {
            "base_url": cls.base_url,
            "sample_mp4": cls.sample_mp4,
        }
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _make_client(self) -> dy_dl.DouyinClient:
        return dy_dl.DouyinClient(
            share_video_tmpl=f"{self.base_url}/share/video/{{item_id}}/",
            share_note_tmpl=f"{self.base_url}/share/note/{{item_id}}/",
            timeout=5,
        )

    def test_extract_url_and_temp_file_input(self) -> None:
        share_copy = (
            f"4.82 复制打开抖音，看看【测试作者的作品】{self.base_url}/short_ok。 "
            "https://v.douyin.com/ignored/ 复制此链接"
        )
        self.assertEqual(dy_dl.extract_url_from_text(share_copy), f"{self.base_url}/short_ok")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_file = Path(tmpdir) / "shared_text.txt"
            tmp_file.write_text(share_copy, encoding="utf-8")
            text = dy_dl.get_input_text([str(tmp_file)])
            self.assertEqual(dy_dl.extract_url_from_text(text), f"{self.base_url}/short_ok")

    def test_extract_ids_and_user_profile_error(self) -> None:
        self.assertEqual(
            dy_dl.extract_ids("https://www.douyin.com/video/7400123456789/"),
            ("video", "7400123456789"),
        )
        self.assertEqual(
            dy_dl.extract_ids("https://www.iesdouyin.com/share/note/7400987654321/?mid=1"),
            ("note", "7400987654321"),
        )
        self.assertEqual(
            dy_dl.extract_ids("https://www.douyin.com/slides/7400987654322/"),
            ("note", "7400987654322"),
        )
        self.assertEqual(
            dy_dl.extract_ids("https://www.douyin.com/discover?modal_id=7400111222333"),
            ("video", "7400111222333"),
        )
        self.assertEqual(
            dy_dl.extract_ids("https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids=7400444555666"),
            ("video", "7400444555666"),
        )

        with self.assertRaises(ValueError) as ctx:
            dy_dl.extract_ids("https://www.douyin.com/user/MS4wLjABAAAAxyz")
        self.assertIn("批量", str(ctx.exception))
        self.assertIn("Cookie", str(ctx.exception))

    def test_sanitize_filename(self) -> None:
        dirty = '标题/带:非法*字符?"<>|\n\t和很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长的尾巴...'
        sanitized = dy_dl.sanitize_filename(dirty, "123456", max_chars=20)
        self.assertTrue(sanitized.endswith("_123456"))
        for bad in '<>:"/\\|?*':
            self.assertNotIn(bad, sanitized)
        self.assertLessEqual(len(sanitized), 20 + len("_123456"))
        self.assertEqual(dy_dl.sanitize_filename("///...", "999"), "douyin_999")

    def test_head_redirect_ttwid_retry_gzip_and_video_download(self) -> None:
        MockDouyinHandler.state["ssr_video_hits"] = 0
        client = self._make_client()

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            dirs = {"video": out_dir, "image": out_dir, "audio": out_dir}
            info, saved = dy_dl.process_url(
                raw_input=f"看这个视频 {self.base_url}/short_ok 复制打开抖音",
                client=client,
                dirs=dirs,
                with_audio=True,
                show_progress=False,
            )

            self.assertGreaterEqual(MockDouyinHandler.state["ssr_video_hits"], 2)
            self.assertEqual(info["kind"], "video")
            self.assertEqual(info["title"], "测试{花括号}视频中文")
            self.assertEqual(info["author"], "测试作者")
            self.assertIn("/media/play?", info["video_url"])
            self.assertNotIn("playwm", info["video_url"])

            self.assertEqual(len(saved), 2)
            video_path, audio_path = saved
            self.assertTrue(video_path.name.endswith("_7400000000000000001.mp4"))
            self.assertTrue(audio_path.name.endswith("_7400000000000000001.mp3"))
            self.assertEqual(video_path.read_bytes(), b"MOCK_NO_WM_MP4_DATA")
            self.assertEqual(audio_path.read_bytes(), b"MOCK_MP3_AUDIO")
            self.assertEqual(list(out_dir.glob("*.part")), [])

    def test_405_get_fallback_gallery_and_audio_only(self) -> None:
        client = self._make_client()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            dirs = {"video": out_dir, "image": out_dir, "audio": out_dir}
            info, saved = dy_dl.process_url(
                raw_input=f"{self.base_url}/short_405",
                client=client,
                dirs=dirs,
                with_audio=True,
                show_progress=False,
            )
            self.assertEqual(info["kind"], "gallery")
            self.assertEqual(len(saved), 3)
            self.assertEqual(saved[0].suffix, ".png")
            self.assertEqual(saved[0].read_bytes(), b"PNG_BYTES")
            self.assertEqual(saved[1].suffix, ".webp")
            self.assertEqual(saved[1].read_bytes(), b"WEBP_BYTES")
            self.assertEqual(saved[2].suffix, ".mp3")
            self.assertEqual(saved[2].read_bytes(), b"MOCK_MP3_AUDIO")

        # Test --audio-only mode
        client2 = self._make_client()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            dirs = {"video": out_dir, "image": out_dir, "audio": out_dir}
            info, saved = dy_dl.process_url(
                raw_input=f"{self.base_url}/short_405",
                client=client2,
                dirs=dirs,
                audio_only=True,
                show_progress=False,
            )
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].suffix, ".mp3")
            self.assertEqual(saved[0].read_bytes(), b"MOCK_MP3_AUDIO")

    def test_animated_note_and_no_merge_fallback(self) -> None:
        client = self._make_client()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            dirs = {"video": out_dir, "image": out_dir, "audio": out_dir}
            info, saved = dy_dl.process_url(
                raw_input=f"{self.base_url}/short_animated",
                client=client,
                dirs=dirs,
                no_merge=True,
                show_progress=False,
            )
            self.assertEqual(info["kind"], "animated")
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0].read_bytes(), b"MOCK_ANIMATED_LIVE_MP4")
            self.assertEqual(saved[1].read_bytes(), b"MOCK_MP3_AUDIO")

        # Also check douyinvod fallback detection
        client2 = self._make_client()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            dirs = {"video": out_dir, "image": out_dir, "audio": out_dir}
            info2, saved2 = dy_dl.process_url(
                raw_input=f"{self.base_url}/short_animated_vod",
                client=client2,
                dirs=dirs,
                no_merge=True,
                show_progress=False,
            )
            self.assertEqual(info2["kind"], "animated")
            self.assertEqual(len(saved2), 1)
            self.assertEqual(saved2[0].read_bytes(), b"MOCK_DOUYINVOD_LIVE_MP4")

    def test_non_strict_router_json_fallback(self) -> None:
        # Simulate trailing comma inside JS object so JSONDecoder falls back to brace matcher
        html = (
            '<html><script>window._ROUTER_DATA = {"desc":"\\u6d4b\\u8bd5",'
            '"images":[{"url_list":["https://img.example.com/1.jpg"]},],'
            '"extra":"https://v.douyin.com/playwm/123",};</script></html>'
        )
        router_dict, router_text = dy_dl.extract_router_data(html)
        self.assertIsNone(router_dict)
        info = dy_dl.parse_content(router_dict, router_text, "video", "888")
        self.assertEqual(info["title"], "测试")
        self.assertEqual(info["video_url"], "https://v.douyin.com/play/123")
        self.assertEqual(info["images"], ["https://img.example.com/1.jpg"])

    def test_download_404_no_retry_and_no_part_leftover(self) -> None:
        client = self._make_client()
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "missing.mp4"
            with mock.patch.object(dy_dl.time, "sleep") as sleep_mock:
                with self.assertRaises(dy_dl.urllib.error.HTTPError):
                    client.download_file(f"{self.base_url}/media/404", dest, show_progress=False)
                sleep_mock.assert_not_called()
            self.assertFalse(dest.exists())
            self.assertEqual(list(Path(tmpdir).glob("*.part")), [])

    def test_video_without_music_url_extracts_audio_via_ffmpeg(self) -> None:
        self.assertTrue(bool(self.sample_mp4), "Expected local ffmpeg to generate sample MP4")

        # 1) --audio-only (dya) extracts MP3/M4A cleanly
        MockDouyinHandler.state["real_av_hits"] = 0
        client = self._make_client()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            dirs = {"video": out_dir, "image": out_dir, "audio": out_dir}
            info, saved = dy_dl.process_url(
                raw_input=f"{self.base_url}/short_no_music",
                client=client,
                dirs=dirs,
                audio_only=True,
                show_progress=False,
            )
            self.assertEqual(info["music_url"], "")
            self.assertEqual(MockDouyinHandler.state["real_av_hits"], 1)
            self.assertEqual(len(saved), 1)
            self.assertIn(saved[0].suffix, (".mp3", ".m4a"))
            self.assertGreater(saved[0].stat().st_size, 0)
            self.assertEqual(list(out_dir.glob("*.part")), [])

        # 2) --with-audio (dy -m) downloads video only once and extracts audio locally
        MockDouyinHandler.state["real_av_hits"] = 0
        client2 = self._make_client()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            dirs = {"video": out_dir, "image": out_dir, "audio": out_dir}
            info2, saved2 = dy_dl.process_url(
                raw_input=f"{self.base_url}/short_no_music",
                client=client2,
                dirs=dirs,
                with_audio=True,
                show_progress=False,
            )
            self.assertEqual(info2["kind"], "video")
            self.assertEqual(MockDouyinHandler.state["real_av_hits"], 1)
            self.assertEqual(len(saved2), 2)
            self.assertEqual(saved2[0].suffix, ".mp4")
            self.assertIn(saved2[1].suffix, (".mp3", ".m4a"))
            self.assertGreater(saved2[1].stat().st_size, 0)
            self.assertEqual(list(out_dir.glob("*.part")), [])

    def test_video_without_music_url_requires_ffmpeg_hint(self) -> None:
        client = self._make_client()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            dirs = {"video": out_dir, "image": out_dir, "audio": out_dir}
            with mock.patch.object(dy_dl.shutil, "which", return_value=None):
                with self.assertRaises(RuntimeError) as ctx:
                    dy_dl.process_url(
                        raw_input=f"{self.base_url}/short_no_music",
                        client=client,
                        dirs=dirs,
                        audio_only=True,
                        show_progress=False,
                    )
                self.assertIn("pkg install -y ffmpeg", str(ctx.exception))

    def test_extract_audio_from_video_m4a_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            video_path = tmp / "input.mp4"
            video_path.write_bytes(b"FAKE_VIDEO")
            dest_mp3 = tmp / "out.mp3"
            calls = []

            def fake_run(cmd, capture_output, text, check):
                calls.append(cmd)
                out_file = Path(cmd[-1])
                if len(calls) == 1:
                    return subprocess.CompletedProcess(cmd, 1, "", "Unknown encoder libmp3lame")
                out_file.write_bytes(b"M4A_STREAM")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(dy_dl.subprocess, "run", side_effect=fake_run):
                result = dy_dl.extract_audio_from_video(video_path, dest_mp3)

            self.assertEqual(len(calls), 2)
            self.assertEqual(result, tmp / "out.m4a")
            self.assertEqual(result.read_bytes(), b"M4A_STREAM")
            self.assertEqual(list(tmp.glob("*.part")), [])

    def test_broader_music_url_extraction(self) -> None:
        # 1) Structured music.audio_track.url_list
        router_dict = {
            "loaderData": {
                "video": {
                    "item_list": [
                        {
                            "desc": "音轨测试",
                            "video": {"play_addr": {"url_list": ["https://v.douyin.com/playwm/1"]}},
                            "music": {
                                "audio_track": {"url_list": ["https://audio.example.com/track.m4a"]}
                            },
                        }
                    ]
                }
            }
        }
        info = dy_dl.parse_content(router_dict, json.dumps(router_dict), "video", "1001")
        self.assertEqual(info["music_url"], "https://audio.example.com/track.m4a")

        # 2) Regex fallback matching .m4a under url_list
        raw_text = (
            '{"desc":"测试","url_list":["https://cdn.example.com/bgm_123.m4a"],'
            '"video":"https://v.douyin.com/playwm/2"}'
        )
        info2 = dy_dl.parse_content(None, raw_text, "video", "1002")
        self.assertEqual(info2["music_url"], "https://cdn.example.com/bgm_123.m4a")

    def test_siliconflow_subtitle_only_and_clipboard_via_main(self) -> None:
        self.assertTrue(bool(self.sample_mp4), "Expected local ffmpeg to generate sample MP4")
        MockDouyinHandler.state["real_av_hits"] = 0
        MockDouyinHandler.state["last_asr_post"] = None

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_dir = tmp / "out"
            cfg_path = tmp / "config.json"

            with mock.patch.object(dy_dl, "DEFAULT_CONFIG_PATH", cfg_path):
                rc_cfg = dy_dl.main(
                    [
                        "--set-key",
                        "test-sf-key",
                        "--asr-url",
                        f"{self.base_url}/siliconflow/v1/audio/transcriptions",
                        "--asr-model",
                        "FunAudioLLM/SenseVoiceSmall",
                    ]
                )
                self.assertEqual(rc_cfg, 0)
                resolved_cfg = dy_dl.resolve_asr_settings()
                self.assertEqual(resolved_cfg["api_key"], "test-sf-key")
                self.assertEqual(
                    resolved_cfg["asr_url"],
                    f"{self.base_url}/siliconflow/v1/audio/transcriptions",
                )

                clipboard_calls = []
                real_client = self._make_client()
                orig_which = shutil.which

                def fake_which(cmd: str) -> str | None:
                    if cmd == "termux-clipboard-set":
                        return "/usr/bin/termux-clipboard-set"
                    if cmd in ("termux-media-scan", "termux-toast", "termux-clipboard-get"):
                        return None
                    return orig_which(cmd)

                orig_run = subprocess.run

                def fake_run(cmd, *args, **kwargs):
                    if cmd and cmd[0] == "termux-clipboard-set":
                        clipboard_calls.append(kwargs.get("input", ""))
                        return subprocess.CompletedProcess(cmd, 0, "", "")
                    return orig_run(cmd, *args, **kwargs)

                with (
                    mock.patch.object(dy_dl, "DouyinClient", return_value=real_client),
                    mock.patch.object(dy_dl.shutil, "which", side_effect=fake_which),
                    mock.patch.object(dy_dl.subprocess, "run", side_effect=fake_run),
                ):
                    rc = dy_dl.main(
                        [
                            "--subtitle-only",
                            "-o",
                            str(out_dir),
                            f"{self.base_url}/short_no_music",
                        ]
                    )

            self.assertEqual(rc, 0)
            self.assertEqual(MockDouyinHandler.state["real_av_hits"], 1)
            post_info = MockDouyinHandler.state["last_asr_post"]
            self.assertIsNotNone(post_info)
            self.assertEqual(post_info["auth"], "Bearer test-sf-key")
            self.assertEqual(post_info["fields"].get("model"), "FunAudioLLM/SenseVoiceSmall")
            self.assertNotIn("response_format", post_info["fields"])
            self.assertGreater(post_info["file_len"], 0)

            self.assertEqual(list(out_dir.glob("*.mp4")), [])
            txt_files = list(out_dir.glob("*.txt"))
            srt_files = list(out_dir.glob("*.srt"))
            self.assertEqual(len(txt_files), 1)
            self.assertEqual(len(srt_files), 1)

            txt_content = txt_files[0].read_text(encoding="utf-8")
            self.assertNotIn("<|zh|>", txt_content)
            self.assertNotIn("<|NEUTRAL|>", txt_content)
            self.assertIn("大家好，今天测试一下手机转录！", txt_content)
            self.assertIn("非常好用。", txt_content)

            srt_content = srt_files[0].read_text(encoding="utf-8")
            self.assertIn("1\n00:00:00,000 --> ", srt_content)
            self.assertIn("大家好，今天测试一下手机转录！", srt_content)
            self.assertEqual(len(clipboard_calls), 1)
            self.assertEqual(clipboard_calls[0], txt_content)
            self.assertEqual(list(out_dir.glob("*.part")), [])

    def test_transcribe_alongside_video_single_download(self) -> None:
        self.assertTrue(bool(self.sample_mp4), "Expected local ffmpeg to generate sample MP4")
        MockDouyinHandler.state["real_av_hits"] = 0
        client = self._make_client()

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            dirs = {"video": out_dir, "image": out_dir, "audio": out_dir, "subtitle": out_dir}
            settings = {
                "api_key": "test-sf-key",
                "asr_url": f"{self.base_url}/siliconflow/v1/audio/transcriptions",
                "asr_model": "FunAudioLLM/SenseVoiceSmall",
            }
            info, saved = dy_dl.process_url(
                raw_input=f"{self.base_url}/short_no_music",
                client=client,
                dirs=dirs,
                transcribe=True,
                asr_settings=settings,
                show_progress=False,
            )

            self.assertEqual(MockDouyinHandler.state["real_av_hits"], 1)
            self.assertTrue(info["srt_estimated"])
            self.assertEqual(len(saved), 3)
            self.assertEqual([p.suffix for p in saved], [".mp4", ".txt", ".srt"])
            self.assertEqual(saved[0].stem, saved[1].stem)
            self.assertEqual(saved[0].stem, saved[2].stem)
            self.assertTrue(all(p.is_file() for p in saved))

    def test_openai_verbose_json_exact_segments(self) -> None:
        self.assertTrue(bool(self.sample_mp4), "Expected local ffmpeg to generate sample MP4")
        MockDouyinHandler.state["last_asr_post"] = None
        client = self._make_client()

        with tempfile.TemporaryDirectory() as tmpdir:
            sub_dir = Path(tmpdir)
            dirs = {"video": sub_dir, "image": sub_dir, "audio": sub_dir, "subtitle": sub_dir}
            settings = {
                "api_key": "gsk_test",
                "asr_url": f"{self.base_url}/openai/v1/audio/transcriptions",
                "asr_model": "whisper-large-v3-turbo",
            }
            info, saved = dy_dl.process_url(
                raw_input=f"{self.base_url}/short_no_music",
                client=client,
                dirs=dirs,
                subtitle_only=True,
                asr_settings=settings,
                show_progress=False,
            )

            post_info = MockDouyinHandler.state["last_asr_post"]
            self.assertEqual(post_info["fields"].get("response_format"), "verbose_json")
            self.assertFalse(info["srt_estimated"])
            self.assertEqual(len(saved), 2)
            srt_content = saved[1].read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:01,250\nHello world.", srt_content)
            self.assertIn("00:00:01,250 --> 00:00:02,500\nSecond line.", srt_content)

    def test_missing_api_key_fails_before_video_download(self) -> None:
        MockDouyinHandler.state["real_av_hits"] = 0
        client = self._make_client()

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            dirs = {"video": out_dir, "image": out_dir, "audio": out_dir, "subtitle": out_dir}
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError) as ctx:
                    dy_dl.process_url(
                        raw_input=f"{self.base_url}/short_no_music",
                        client=client,
                        dirs=dirs,
                        subtitle_only=True,
                        asr_settings={"api_key": "", "asr_url": dy_dl.DEFAULT_ASR_URL, "asr_model": dy_dl.DEFAULT_ASR_MODEL},
                        show_progress=False,
                    )
            self.assertIn("--set-key", str(ctx.exception))
            self.assertEqual(MockDouyinHandler.state["real_av_hits"], 0)


if __name__ == "__main__":
    unittest.main()
