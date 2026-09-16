# -*- coding: utf-8 -*-
"""Remote Screen Companion Client.

默认按需截图：客户端连接后只保持连接与保活，不主动采集、不上传图片；
只有在插件通过 WebSocket 请求截图时才立即采集一张新图并回传。

Usage:
    python remote_client.py --server ws://your-server:6315 --token your-token

协议版本 2 新增能力协商与按需截图；旧服务端仍可通过 ``--push`` 使用原有的
周期推送兼容模式。详见 README 的「远程识屏模式」一节。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import math
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

pyautogui = None

try:
    import websockets
    import websockets.exceptions
except ImportError:
    print("ERROR: pip install websockets")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("screen_client")


# --------------------------------------------------------------------------
# 协议常量
# --------------------------------------------------------------------------

PROTOCOL_VERSION = 2
CAPABILITY_REQUEST_SCREENSHOT = "request_screenshot"

#: 收到按需请求后，本次采集与上传允许使用的总预算（秒）。
REQUEST_BUDGET_SECONDS = 10.0
#: 单条控制消息（busy / capture_error 等）的发送预算（秒）。
CONTROL_SEND_TIMEOUT = 1.0
#: 单个应用层 ACK 交换的等待上限（秒）。
ACK_TIMEOUT_SECONDS = 15.0
#: 视频相关 ACK 的等待上限（秒）。
VIDEO_ACK_TIMEOUT_SECONDS = 20.0
#: 握手各阶段的等待上限（秒）。
HANDSHAKE_TIMEOUT_SECONDS = 10.0
#: 窗口标题查询预算：单次与单轮总量（秒）。
TITLE_PER_CALL_SECONDS = 0.5
TITLE_TOTAL_SECONDS = 1.0
#: 客户端错误文本长度上限。
MAX_ERROR_TEXT_LENGTH = 200

#: 客户端错误码。
CLIENT_ERROR_BUSY = "busy"
CLIENT_ERROR_TIMEOUT = "timeout"
CLIENT_ERROR_UNSUPPORTED = "unsupported_client"
CLIENT_ERROR_CAPTURE_FAILED = "capture_failed"


class ClientConfigError(ValueError):
    """配置或参数组合不合法：属于确定的配置错误，应直接退出。"""


class IncompatibleServerError(RuntimeError):
    """服务端不支持按需截图，且当前参数组合无法在旧协议下工作。"""


class _SessionClosedError(RuntimeError):
    """会话已经关闭，不能再发送或等待响应。"""


class _CaptureBusyError(RuntimeError):
    """上一次底层截图线程仍未结束，本次不能开始新的采集。"""


class _CaptureBudgetExceededError(RuntimeError):
    """本次请求的本地采集预算已耗尽。"""


class _ProtocolError(RuntimeError):
    """协议序列损坏：必须结束当前会话后重连。"""


# --------------------------------------------------------------------------
# 进程内截图线程守卫
# --------------------------------------------------------------------------


class _CaptureSlot:
    """保证进程内最多只有一个尚未结束的截图线程。

    取消 ``asyncio.to_thread`` 不会终止底层线程，因此槽位由工作线程自身在
    真正结束时释放；期间新的采集请求一律判为繁忙。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held = False

    def try_acquire(self) -> bool:
        with self._lock:
            if self._held:
                return False
            self._held = True
            return True

    def release(self) -> None:
        with self._lock:
            self._held = False

    @property
    def held(self) -> bool:
        with self._lock:
            return self._held


_CAPTURE_SLOT = _CaptureSlot()


def _run_guarded_capture(func, *args, **kwargs):
    """在线程中执行采集，并在真正结束时释放进程内截图槽位。"""
    try:
        return func(*args, **kwargs)
    finally:
        _CAPTURE_SLOT.release()


# --------------------------------------------------------------------------
# 采集辅助
# --------------------------------------------------------------------------


class _TitleBudget:
    """限制一轮采集中所有窗口标题查询的总耗时。"""

    def __init__(
        self,
        total_seconds: float = TITLE_TOTAL_SECONDS,
        per_call_seconds: float = TITLE_PER_CALL_SECONDS,
    ) -> None:
        self.total_seconds = float(total_seconds)
        self.per_call_seconds = float(per_call_seconds)
        self._spent = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_seconds - self._spent)

    def consume(self, seconds: float) -> None:
        self._spent += max(0.0, float(seconds))


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, float(deadline) - time.monotonic())


def _ensure_not_expired(deadline: float) -> None:
    if _remaining_seconds(deadline) <= 0:
        raise _CaptureBudgetExceededError("本地采集预算已耗尽")


def _query_active_window_title(timeout: float) -> tuple[str, bool]:
    """带超时查询活动窗口标题。

    返回 ``(标题, 是否成功)``：查询失败与"成功得到空标题"必须区分，
    否则调用方会把失败误判成标题没变而跳过补拍。
    """
    budget = max(0.0, float(timeout))
    if budget <= 0:
        return "", False
    try:
        if sys.platform == "win32":
            import pygetwindow

            win = pygetwindow.getActiveWindow()
            return (str(win.title or "").strip() if win else ""), True
        if sys.platform == "darwin":
            script = (
                "tell application \"System Events\" to get name of first application "
                "process whose frontmost is true"
            )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=budget,
            )
            return result.stdout.strip(), True

        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True,
            text=True,
            timeout=budget,
        )
        return result.stdout.strip(), True
    except Exception as e:
        log.debug(f"Failed to get window title: {e}")
        return "", False


def get_active_window_title() -> str:
    """获取活动窗口标题；查询失败时返回空字符串。"""
    title, _ok = _query_active_window_title(TITLE_PER_CALL_SECONDS)
    return title


def _get_title_with_budget(
    budget: _TitleBudget, deadline: float
) -> tuple[str, bool]:
    """在标题预算与请求预算内查询一次窗口标题。"""
    remaining = min(budget.per_call_seconds, budget.remaining, _remaining_seconds(deadline))
    if remaining <= 0:
        return "", False
    started = time.monotonic()
    title, ok = _query_active_window_title(remaining)
    budget.consume(time.monotonic() - started)
    return title, ok


def get_system_stats() -> dict[str, Any]:
    """采集可选系统统计；非阻塞，失败时返回已完成的部分结果。"""
    stats: dict[str, Any] = {}
    try:
        import psutil

        # interval=None 使用自上次调用以来的差值，不引入额外等待。
        stats["cpu_percent"] = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        stats["memory_percent"] = mem.percent
        stats["memory_used_mb"] = mem.used // (1024 * 1024)
        try:
            battery = psutil.sensors_battery()
        except Exception as e:  # 某些平台没有电池接口
            log.debug(f"读取电池信息失败: {e}")
            battery = None
        if battery:
            stats["battery_percent"] = battery.percent
            stats["battery_plugged"] = battery.power_plugged
    except ImportError:
        pass
    except Exception as e:
        log.debug(f"读取系统统计失败: {e}")
    return stats


def capture_screenshot(image_quality: int = 70) -> bytes:
    global pyautogui
    if pyautogui is None:
        try:
            import pyautogui as pyautogui_module
        except ImportError as exc:
            raise RuntimeError("截图模式需要 pyautogui 和 Pillow，请先安装客户端依赖") from exc
        pyautogui = pyautogui_module
    screenshot = pyautogui.screenshot()
    if screenshot.mode != "RGB":
        screenshot = screenshot.convert("RGB")
    buf = io.BytesIO()
    screenshot.save(buf, format="JPEG", quality=image_quality)
    return buf.getvalue()


def capture_screenshot_context(
    image_quality: int = 70,
    deadline: float | None = None,
    *,
    include_stats: bool = False,
) -> tuple[bytes, str, dict[str, Any]]:
    """一次同步调用内完成窗口标题配对与截图。

    这是同步函数，必须在同一个线程中执行：标题查询与截图之间的间隔越短，
    "图片与窗口信息错配"的概率就越低。可选系统统计不参与按需路径，
    避免 CPU/电池采样拖慢结果。
    """
    if deadline is None:
        deadline = time.monotonic() + REQUEST_BUDGET_SECONDS
    title_budget = _TitleBudget()
    title = ""
    captured_at = time.time()
    jpeg = b""

    for _attempt in range(2):
        _ensure_not_expired(deadline)
        before, before_ok = _get_title_with_budget(title_budget, deadline)
        captured_at = time.time()
        jpeg = capture_screenshot(image_quality)
        after, after_ok = _get_title_with_budget(title_budget, deadline)
        if not before_ok or not after_ok:
            # 可选元数据失败时不补拍、不延长请求。
            title = ""
            break
        if before == after:
            title = after
            break
    else:  # pragma: no cover - 循环内必定 break
        title = ""

    _ensure_not_expired(deadline)
    stats = get_system_stats() if include_stats else {}
    meta: dict[str, Any] = {"timestamp": captured_at, "system_stats": stats}
    return jpeg, title, meta


def capture_video(duration_seconds: int, ffmpeg_path: str = "") -> bytes:
    """Capture a short MP4 clip using the platform's ffmpeg input."""
    duration = max(1, int(duration_seconds or 10))
    ffmpeg = ffmpeg_path.strip() or ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    output_path = os.path.join(tempfile.gettempdir(), f"screen_companion_{uuid.uuid4().hex}.mp4")
    if sys.platform == "win32":
        input_args = ["-f", "gdigrab", "-framerate", "5", "-i", "desktop"]
    elif sys.platform == "darwin":
        input_args = ["-f", "avfoundation", "-framerate", "5", "-i", "1:none"]
    else:
        display = os.environ.get("DISPLAY", ":0")
        input_args = ["-f", "x11grab", "-framerate", "5", "-i", f"{display}.0"]
    command = [ffmpeg, "-y", *input_args, "-t", str(duration), "-pix_fmt", "yuv420p", output_path]
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=duration + 20,
        )
        with open(output_path, "rb") as video_file:
            return video_file.read()
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# 客户端配置
# --------------------------------------------------------------------------


@dataclass
class ClientConfig:
    """经过校验的客户端运行时配置。"""

    server_url: str
    token: str = ""
    client_id: str = ""
    image_quality: int = 70
    interval: float = 10.0
    binary: bool = True
    push_enabled: bool = False
    heartbeat_interval: float = 30.0
    video_enabled: bool = False
    screenshot_enabled: bool = True
    video_only: bool = False
    screenshot_only: bool = False
    video_duration: int = 10
    ffmpeg_path: str = ""
    request_budget: float = REQUEST_BUDGET_SECONDS
    ack_timeout: float = ACK_TIMEOUT_SECONDS
    video_ack_timeout: float = VIDEO_ACK_TIMEOUT_SECONDS
    reconnect_delay: float = 5.0
    refused_delay: float = 10.0

    def describe_modes(self) -> list[str]:
        lines = []
        lines.append(
            "周期截图：" + ("启用（--push）" if self.push_enabled else "关闭")
        )
        lines.append(
            "按需截图："
            + (
                "启用"
                if self.screenshot_enabled and not self.video_only
                else "关闭"
            )
        )
        lines.append("周期录屏：" + ("启用" if self.video_enabled else "关闭"))
        if self.video_enabled and self.push_enabled and self.video_only:
            lines.append("提示：--push 对 --video-only 无效，纯视频模式不上传截图")
        if self.screenshot_only and self.video_enabled is False:
            lines.append("提示：--screenshot-only 优先，已关闭 --video 的周期录屏")
        return lines


def validate_config(args: argparse.Namespace) -> ClientConfig:
    """把命令行参数转换为经过校验的配置；非法组合直接抛错。"""
    quality = int(args.quality)
    if not (1 <= quality <= 100):
        raise ClientConfigError("--quality 必须是 1~100 之间的整数")

    interval = float(args.interval)
    if not math.isfinite(interval) or interval < 0.1:
        raise ClientConfigError("--interval 必须是大于等于 0.1 的有限数值")

    heartbeat = float(args.heartbeat)
    if not math.isfinite(heartbeat) or heartbeat < 0:
        raise ClientConfigError("--heartbeat 必须是大于等于 0 的有限数值（0 表示关闭主动保活）")

    video_duration = int(args.video_duration)
    if video_duration <= 0:
        raise ClientConfigError("--video-duration 必须是正整数")

    video_only = bool(args.video_only)
    screenshot_only = bool(args.screenshot_only)
    video_enabled = bool(args.video or video_only) and not screenshot_only
    screenshot_enabled = not video_only

    use_binary = bool(args.binary) or not bool(getattr(args, "json_output", False))

    return ClientConfig(
        server_url=str(args.server or "").strip(),
        token=str(args.token or ""),
        client_id=str(args.client_id or "").strip() or f"client_{platform.node()}",
        image_quality=quality,
        interval=interval,
        binary=use_binary,
        push_enabled=bool(args.push),
        heartbeat_interval=heartbeat,
        video_enabled=video_enabled,
        screenshot_enabled=screenshot_enabled,
        video_only=video_only,
        screenshot_only=screenshot_only,
        video_duration=video_duration,
        ffmpeg_path=str(args.ffmpeg_path or ""),
    )


@dataclass
class _AckWaiter:
    """至多一个尚未完成的应用层 ACK 交换。"""

    kind: str
    expected_status: str
    future: "asyncio.Future[dict[str, Any]]"
    request_id: str = ""
    upload_id: str = ""
    index: int | None = None


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------


class RemoteClientSession:
    """单个 WebSocket 连接上的会话。

    ``RemoteClientSession`` 只绑定一个连接：重连必须新建实例，不能把上次连接的
    截图任务、ACK Future 或录像上传状态转移到新会话。
    """

    def __init__(
        self,
        websocket,
        config: ClientConfig,
        *,
        server_handshake: dict[str, Any] | None = None,
    ) -> None:
        self._ws = websocket
        self._config = config
        self._server_handshake = dict(server_handshake or {})
        self._legacy_server = False
        self._negotiated = False
        self._closed = False
        self._send_lock = asyncio.Lock()
        self._ack_waiter: _AckWaiter | None = None
        # 单个作业占用：按需截图、显式周期推送、录像上传共用。
        self._job_busy = False
        self._active_request_id = ""
        self._reader_task: asyncio.Task | None = None
        self._capture_task: asyncio.Task | None = None
        self._push_task: asyncio.Task | None = None
        self._video_task: asyncio.Task | None = None
        self.disconnect_reason = ""

    # ------------------------------------------------------------------
    # 能力与状态
    # ------------------------------------------------------------------

    @property
    def legacy_server(self) -> bool:
        return self._legacy_server

    @property
    def negotiated(self) -> bool:
        return self._negotiated

    @property
    def job_busy(self) -> bool:
        return self._job_busy

    @property
    def active_request_id(self) -> str:
        return self._active_request_id

    # ------------------------------------------------------------------
    # 运行入口
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """运行会话直到连接结束；返回后会话状态已清理完毕。"""
        loop = asyncio.get_running_loop()
        self._reader_task = loop.create_task(self._receive_loop())
        try:
            await self._negotiate()
            if self._config.screenshot_enabled and self._config.push_enabled:
                self._push_task = loop.create_task(self._push_loop())
            if self._config.video_enabled:
                self._video_task = loop.create_task(self._video_loop())
            await self._reader_task
        finally:
            await self._close_session()

    async def _negotiate(self) -> None:
        """校验服务端能力并声明客户端能力。"""
        server_protocol = 0
        try:
            server_protocol = int(self._server_handshake.get("protocol_version", 0) or 0)
        except (TypeError, ValueError):
            server_protocol = 0
        raw_caps = self._server_handshake.get("capabilities", [])
        if isinstance(raw_caps, str):
            raw_caps = [raw_caps]
        server_caps = {
            str(item).strip()
            for item in (raw_caps if isinstance(raw_caps, (list, tuple, set)) else [])
            if str(item).strip()
        }
        self._legacy_server = not (
            server_protocol >= PROTOCOL_VERSION
            or CAPABILITY_REQUEST_SCREENSHOT in server_caps
        )

        if self._legacy_server:
            if not (self._config.push_enabled or self._config.video_only):
                raise IncompatibleServerError(
                    "服务端不支持按需截图（协议 v2）。请先升级插件端；"
                    "如果暂时无法升级，可显式加 --push 使用周期截图兼容模式，"
                    "纯录屏可用 --video-only。"
                )
            log.warning(
                "服务端为旧协议：按需截图不可用，本客户端以兼容模式运行"
            )
            self._negotiated = True
            return

        capabilities = (
            [CAPABILITY_REQUEST_SCREENSHOT] if self._config.screenshot_enabled else []
        )
        await self._exchange(
            {
                "type": "client_capabilities",
                "protocol_version": PROTOCOL_VERSION,
                "client_id": self._config.client_id,
                "capabilities": capabilities,
            },
            expected_status="capabilities_received",
            kind="handshake",
            timeout=HANDSHAKE_TIMEOUT_SECONDS,
        )
        log.info(
            "能力协商完成：capabilities=%s",
            capabilities or "[]（纯视频模式，不接收截图请求）",
        )

    # ------------------------------------------------------------------
    # 接收循环（唯一读取应用消息的位置）
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                await self._dispatch(raw)
        except websockets.exceptions.ConnectionClosed as e:
            self.disconnect_reason = f"连接已关闭: {e}"
        except _ProtocolError as e:
            self.disconnect_reason = f"协议错误: {e}"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - 防御性
            self.disconnect_reason = f"接收异常: {e}"
        finally:
            self._fail_current_waiter(
                _SessionClosedError(self.disconnect_reason or "连接已结束")
            )

    async def _dispatch(self, raw) -> None:
        if isinstance(raw, (bytes, bytearray)):
            # 服务端不向客户端发送二进制消息；忽略但记录有限诊断。
            log.debug("忽略服务端二进制消息")
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.debug("忽略无法解析的服务端消息")
            return
        if not isinstance(data, dict):
            return

        msg_type = str(data.get("type", "") or "")

        if msg_type == "request_screenshot":
            await self._handle_screenshot_request(data)
            return

        if msg_type == "pong":
            return

        if "status" in data or "error" in data:
            self._resolve_ack(data)
            return

        log.debug("忽略未知服务端消息类型: %s", msg_type or "<empty>")

    def _resolve_ack(self, data: dict[str, Any]) -> None:
        """把一条 status/error 消息分发给唯一在等的 ACK waiter。"""
        waiter = self._ack_waiter
        if waiter is None:
            log.debug("收到无归属的服务端消息，已忽略: %s", data)
            return

        status = str(data.get("status", "") or "")
        if status and self._waiter_matches(waiter, data):
            # 先同步摘下 waiter，再完成 Future，避免旧协程收尾误清新 waiter。
            self._ack_waiter = None
            if status == "capabilities_received":
                # 服务端可能在能力 ACK 后立即发来首个截图请求，因此必须在这里
                # 同步标记协商完成，不能等协商协程下一次获得调度。
                self._negotiated = True
            if not waiter.future.done():
                waiter.future.set_result(data)
            return

        if "error" in data:
            if self._error_matches(waiter, data):
                self._ack_waiter = None
                if not waiter.future.done():
                    waiter.future.set_exception(
                        _ProtocolError(
                            f"服务端拒绝 {waiter.kind} 交换: {data.get('error')}"
                        )
                    )
                return
            # 关联字段不符，无法确定归属：结束会话，避免旧 ACK 被下一次上传误接收。
            raise _ProtocolError(f"服务端错误无法归属: {data}")

        if status:
            # 不匹配的 status 不完成当前 waiter，只记录有限诊断后丢弃。
            # 只有无法归属的错误或发送/等待失败才会终止会话。
            log.debug(
                "忽略不匹配的服务端状态: 期望 %s，实际 %s（关联字段 %s）",
                waiter.expected_status,
                status,
                {
                    key: data.get(key)
                    for key in ("request_id", "upload_id", "index")
                    if key in data
                },
            )
            return

    def _waiter_matches(self, waiter: _AckWaiter, data: dict[str, Any]) -> bool:
        status = str(data.get("status", "") or "")
        if status != waiter.expected_status:
            return False
        if waiter.upload_id:
            ack_upload_id = str(data.get("upload_id", "") or "")
            if waiter.kind == "video_chunk":
                # v2 服务端会回显 upload_id；旧服务端只回显 index，继续兼容。
                if ack_upload_id and ack_upload_id != waiter.upload_id:
                    return False
            elif ack_upload_id != waiter.upload_id:
                return False
        if waiter.kind == "video_chunk":
            try:
                return int(data.get("index", -1)) == int(waiter.index or 0)
            except (TypeError, ValueError):
                return False
        if waiter.request_id:
            return str(data.get("request_id", "") or "") == waiter.request_id
        # 无请求编号的事务：ACK 不应携带 request_id，否则可能被误配。
        return not str(data.get("request_id", "") or "")

    def _error_matches(self, waiter: _AckWaiter, data: dict[str, Any]) -> bool:
        ack_request_id = str(data.get("request_id", "") or "")
        if waiter.request_id:
            return ack_request_id == waiter.request_id
        return not ack_request_id

    def _fail_current_waiter(self, exc: Exception) -> None:
        waiter = self._ack_waiter
        if waiter is None:
            return
        self._ack_waiter = None
        if not waiter.future.done():
            try:
                waiter.future.set_exception(exc)
            except (asyncio.InvalidStateError, RuntimeError):  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def _send_message(
        self, payload, *, timeout: float = CONTROL_SEND_TIMEOUT
    ) -> None:
        """所有应用消息的统一发送通道。

        短锁只保护一次完整的 ``ws.send``，绝不跨图像事务或 ACK 等待持有，
        因此繁忙回复可以在既有作业等待 ACK 时发出。
        """
        if self._closed:
            raise _SessionClosedError("会话已关闭，不能发送消息")
        data = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload)
        async with self._send_lock:
            if self._closed:
                raise _SessionClosedError("会话已关闭，不能发送消息")
            await asyncio.wait_for(self._ws.send(data), timeout=timeout)

    async def _send_control(self, payload: dict[str, Any]) -> bool:
        """有界发送一条无需 ACK 的控制消息；失败时结束会话。"""
        try:
            await self._send_message(payload, timeout=CONTROL_SEND_TIMEOUT)
            return True
        except _SessionClosedError:
            return False
        except asyncio.TimeoutError:
            log.warning("控制消息发送超时（网络背压），结束当前会话")
            await self._close_transport()
            return False
        except Exception as e:
            log.warning(f"控制消息发送失败，结束当前会话: {e}")
            await self._close_transport()
            return False

    async def _exchange(
        self,
        payload,
        *,
        expected_status: str,
        kind: str,
        request_id: str = "",
        upload_id: str = "",
        index: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """先登记 ACK Future，再发送，最后等待匹配的状态消息。

        本函数不调用 ``recv()``：读取永远只发生在 ``_receive_loop()``。
        """
        if timeout is None:
            timeout = (
                self._config.video_ack_timeout
                if kind.startswith("video_")
                else self._config.ack_timeout
            )
        if self._ack_waiter is not None:
            raise _ProtocolError("已有在途 ACK 交换，不能并发发起第二个")
        loop = asyncio.get_running_loop()
        waiter = _AckWaiter(
            kind=kind,
            expected_status=expected_status,
            future=loop.create_future(),
            request_id=request_id,
            upload_id=upload_id,
            index=index,
        )
        # 必须先登记再发送：服务端可能在 send() 返回前就回 ACK。
        self._ack_waiter = waiter
        deadline = loop.time() + float(timeout)
        try:
            await self._send_message(
                payload,
                timeout=min(
                    max(0.0, deadline - loop.time()),
                    self._config.ack_timeout,
                ),
            )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            return await asyncio.wait_for(waiter.future, timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise _ProtocolError(
                f"等待 {expected_status} 超时，结束会话以避免旧 ACK 错配"
            ) from exc
        finally:
            if self._ack_waiter is waiter:
                self._ack_waiter = None
            if not waiter.future.done():
                waiter.future.cancel()
            try:
                waiter.future.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                pass
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 按需截图
    # ------------------------------------------------------------------

    @staticmethod
    def _valid_request_id(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        candidate = value.strip()
        if not candidate or len(candidate) > 64:
            return ""
        if not all(ch.isalnum() or ch in "_-" for ch in candidate):
            return ""
        return candidate

    async def _handle_screenshot_request(self, data: dict[str, Any]) -> None:
        """处理一次按需截图请求。

        从校验到占用状态之间不允许出现 ``await``，否则周期任务可能在此抢占
        作业状态，造成两次并发采集。
        """
        request_id = self._valid_request_id(data.get("request_id"))
        if not request_id:
            await self._send_control({
                "type": "capture_error",
                "code": CLIENT_ERROR_CAPTURE_FAILED,
                "error": "缺少或非法的 request_id",
            })
            return

        if not self._config.screenshot_enabled:
            await self._send_control({
                "type": "capture_error",
                "request_id": request_id,
                "code": CLIENT_ERROR_UNSUPPORTED,
                "error": "当前客户端模式不支持按需截图",
            })
            return

        if self._active_request_id and self._active_request_id == request_id:
            # 重复的同一个请求：忽略，不二次采集，也不回复会失败原请求的错误。
            log.debug("忽略重复的截图请求: %s", request_id)
            return

        if self._job_busy or self._active_request_id:
            await self._send_control({
                "type": "capture_error",
                "request_id": request_id,
                "code": CLIENT_ERROR_BUSY,
                "error": "已有采集或上传任务正在进行",
            })
            return

        if self._legacy_server:
            await self._send_control({
                "type": "capture_error",
                "request_id": request_id,
                "code": CLIENT_ERROR_UNSUPPORTED,
                "error": "服务端为旧协议，不支持按需截图",
            })
            return

        deadline = time.monotonic() + float(self._config.request_budget)
        # 同步预留作业状态：这一步到设置占用之间不能有 await。
        self._job_busy = True
        self._active_request_id = request_id
        self._capture_task = asyncio.get_running_loop().create_task(
            self._capture_and_send(request_id, deadline)
        )

    async def _capture_and_send(self, request_id: str, deadline: float) -> None:
        """接受请求后立即重拍，再完成整帧发送事务。"""
        try:
            jpeg_bytes, title, meta = await self._capture_now(deadline)
            if _remaining_seconds(deadline) <= 0:
                raise _CaptureBudgetExceededError("采集完成时预算已耗尽")
            await self._send_screenshot_payload(
                jpeg_bytes,
                title,
                meta,
                request_id=request_id,
                deadline=deadline,
            )
        except asyncio.CancelledError:
            raise
        except _ProtocolError as e:
            # ACK 超时或关联字段损坏：本会话不能继续复用，必须重连。
            log.warning("截图事务协议错误，结束当前会话: %s", e)
            await self._close_transport()
        except _CaptureBusyError:
            await self._send_control({
                "type": "capture_error",
                "request_id": request_id,
                "code": CLIENT_ERROR_BUSY,
                "error": "上一次截图线程仍未结束",
            })
        except _CaptureBudgetExceededError as e:
            await self._send_control({
                "type": "capture_error",
                "request_id": request_id,
                "code": CLIENT_ERROR_TIMEOUT,
                "error": str(e)[:MAX_ERROR_TEXT_LENGTH],
            })
        except _SessionClosedError:
            # 会话已结束：不再尝试发送任何错误。
            log.debug("会话已关闭，丢弃请求 %s 的结果", request_id)
        except Exception as e:
            log.error("按需截图失败: %s", e)
            await self._send_control({
                "type": "capture_error",
                "request_id": request_id,
                "code": CLIENT_ERROR_CAPTURE_FAILED,
                "error": str(e)[:MAX_ERROR_TEXT_LENGTH],
            })
        finally:
            if self._active_request_id == request_id:
                self._active_request_id = ""
            self._job_busy = False
            self._capture_task = None

    async def _capture_now(
        self, deadline: float, *, include_stats: bool = False
    ) -> tuple[bytes, str, dict[str, Any]]:
        """在线程中执行一次同步采集，受进程内单线程槽位约束。"""
        _ensure_not_expired(deadline)
        if not _CAPTURE_SLOT.try_acquire():
            raise _CaptureBusyError("上一次截图线程仍未结束")
        try:
            return await asyncio.to_thread(
                _run_guarded_capture,
                capture_screenshot_context,
                self._config.image_quality,
                deadline,
                include_stats=include_stats,
            )
        except asyncio.CancelledError:
            # 线程仍在运行，由工作线程自行释放槽位；其结果不会被本会话发送。
            raise
        except Exception:
            if _CAPTURE_SLOT.held:
                _CAPTURE_SLOT.release()
            raise

    async def _send_screenshot_payload(
        self,
        jpeg_bytes: bytes,
        window_title: str,
        meta: dict[str, Any],
        *,
        request_id: str = "",
        deadline: float | None = None,
    ) -> None:
        """完成一次完整的图像上传事务。"""
        payload_base: dict[str, Any] = {
            "window_title": window_title,
            "system_stats": meta.get("system_stats", {}),
            "timestamp": meta.get("timestamp", time.time()),
            "client_id": self._config.client_id,
        }
        if request_id:
            payload_base["request_id"] = request_id

        def exchange_timeout() -> float | None:
            if deadline is None:
                return None
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                raise _CaptureBudgetExceededError("截图上传时预算已耗尽")
            return min(float(self._config.ack_timeout), remaining)

        if self._config.binary:
            await self._exchange(
                {"type": "screenshot_meta", **payload_base},
                expected_status="meta_received",
                kind="screenshot_meta",
                request_id=request_id,
                timeout=exchange_timeout(),
            )
            await self._exchange(
                jpeg_bytes,
                expected_status="binary_screenshot_received",
                kind="screenshot_binary",
                request_id=request_id,
                timeout=exchange_timeout(),
            )
        else:
            await self._exchange(
                {
                    "type": "screenshot_bundle",
                    "image": base64.b64encode(jpeg_bytes).decode("ascii"),
                    **payload_base,
                },
                expected_status="screenshot_received",
                kind="screenshot_bundle",
                request_id=request_id,
                timeout=exchange_timeout(),
            )

    # ------------------------------------------------------------------
    # 显式周期任务
    # ------------------------------------------------------------------

    async def _push_loop(self) -> None:
        """显式 ``--push`` 的周期截图上传；与按需截图共享单个作业占用。"""
        log.info("周期截图已启用（--push），间隔 %.1f 秒", self._config.interval)
        while not self._closed:
            try:
                await asyncio.sleep(self._config.interval)
                if self._closed:
                    return
                if self._job_busy:
                    log.debug("作业占用中，跳过本轮周期截图")
                    continue
                self._job_busy = True
                try:
                    deadline = time.monotonic() + max(
                        float(self._config.request_budget), 30.0
                    )
                    jpeg_bytes, title, meta = await self._capture_now(
                        deadline, include_stats=True
                    )
                    await self._send_screenshot_payload(jpeg_bytes, title, meta)
                finally:
                    self._job_busy = False
            except asyncio.CancelledError:
                raise
            except (_SessionClosedError, _ProtocolError) as e:
                log.warning("周期截图结束: %s", e)
                await self._close_transport()
                return
            except Exception as e:
                log.error("周期截图失败: %s", e)

    async def _video_loop(self) -> None:
        """显式录像模式：周期性录制并上传短视频。"""
        log.info("周期录屏已启用，时长 %s 秒", self._config.video_duration)
        while not self._closed:
            try:
                await asyncio.sleep(self._config.interval)
                if self._closed:
                    return
                if self._job_busy:
                    log.debug("作业占用中，跳过本轮录屏")
                    continue
                self._job_busy = True
                try:
                    video_bytes = await asyncio.to_thread(
                        capture_video,
                        self._config.video_duration,
                        self._config.ffmpeg_path,
                    )
                    await self._send_video(video_bytes)
                finally:
                    self._job_busy = False
            except asyncio.CancelledError:
                raise
            except (_SessionClosedError, _ProtocolError) as e:
                log.warning("周期录屏结束: %s", e)
                await self._close_transport()
                return
            except Exception as e:
                log.error("周期录屏失败: %s", e)

    async def _send_video(self, video_bytes: bytes) -> None:
        """按既有协议分块上传一段视频；确认只由统一 reader 读取。"""
        upload_id = uuid.uuid4().hex
        chunk_size = 5 * 1024 * 1024
        await self._exchange(
            {
                "type": "video_meta",
                "upload_id": upload_id,
                "total_size": len(video_bytes),
                "mime_type": "video/mp4",
                "window_title": get_active_window_title(),
                "client_id": self._config.client_id,
                "duration_seconds": max(0, int(self._config.video_duration or 0)),
                "timestamp": time.time(),
            },
            expected_status="video_ready",
            kind="video_meta",
            upload_id=upload_id,
        )
        for index, offset in enumerate(range(0, len(video_bytes), chunk_size)):
            chunk = video_bytes[offset : offset + chunk_size]
            await self._exchange(
                {
                    "type": "video_chunk",
                    "upload_id": upload_id,
                    "index": index,
                    "data": base64.b64encode(chunk).decode("ascii"),
                },
                expected_status="video_chunk_received",
                kind="video_chunk",
                upload_id=upload_id,
                index=index,
            )
        await self._exchange(
            {"type": "video_complete", "upload_id": upload_id},
            expected_status="video_complete",
            kind="video_complete",
            upload_id=upload_id,
        )
        log.info("已上传录屏: %s bytes", len(video_bytes))

    # ------------------------------------------------------------------
    # 会话收尾
    # ------------------------------------------------------------------

    async def _close_transport(self) -> None:
        """主动关闭传输，唤醒 reader，使会话尽快结束。"""
        try:
            await self._ws.close()
        except Exception:
            pass

    async def _close_session(self) -> None:
        """终止等待者、取消并等待所有协程、关闭连接并清理本会话状态。"""
        self._closed = True
        self._fail_current_waiter(_SessionClosedError("会话已关闭"))
        current = asyncio.current_task()
        pending = [
            task
            for task in (self._capture_task, self._push_task, self._video_task)
            if task is not None and task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        reader = self._reader_task
        if reader is not None and reader is not current and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        try:
            await self._ws.close()
        except Exception:
            pass
        self._capture_task = None
        self._push_task = None
        self._video_task = None
        self._active_request_id = ""
        self._job_busy = False


# --------------------------------------------------------------------------
# 连接循环
# --------------------------------------------------------------------------


async def run_client(config: ClientConfig) -> None:
    """连接服务端并维持会话，直到进程被中断。"""
    ping_interval = config.heartbeat_interval if config.heartbeat_interval > 0 else None
    while True:
        try:
            log.info(f"Connecting to {config.server_url} ...")
            async with websockets.connect(
                config.server_url,
                ping_interval=ping_interval,
                close_timeout=1.0,
            ) as ws:
                handshake: dict[str, Any] = {}
                if config.token:
                    await ws.send(json.dumps({"token": config.token}))
                    resp = json.loads(await asyncio.wait_for(
                        ws.recv(), timeout=HANDSHAKE_TIMEOUT_SECONDS
                    ))
                    if resp.get("status") not in {"authenticated", "ready"}:
                        log.error(f"Auth failed: {resp}")
                        await asyncio.sleep(config.reconnect_delay)
                        continue
                    handshake = resp
                    log.info(f"Server status: {resp.get('status')}")
                else:
                    resp = json.loads(await asyncio.wait_for(
                        ws.recv(), timeout=HANDSHAKE_TIMEOUT_SECONDS
                    ))
                    handshake = resp
                    log.info(f"Server status: {resp.get('status')}")

                session = RemoteClientSession(ws, config, server_handshake=handshake)
                try:
                    await session.run()
                except IncompatibleServerError:
                    raise
                log.info("会话结束，准备重新连接")
        except IncompatibleServerError:
            # 参数或能力不兼容属于确定的配置错误：不要无限重连同一个服务。
            raise
        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"Connection closed: {e}, reconnecting in {config.reconnect_delay}s...")
            await asyncio.sleep(config.reconnect_delay)
        except ConnectionRefusedError:
            log.warning(f"Connection refused, retrying in {config.refused_delay}s...")
            await asyncio.sleep(config.refused_delay)
        except OSError as e:
            log.warning(f"网络不可用: {e}，{config.refused_delay}s 后重试")
            await asyncio.sleep(config.refused_delay)
        except Exception as e:
            log.error(f"Unexpected error: {e}, reconnecting in {config.refused_delay}s...")
            await asyncio.sleep(config.refused_delay)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remote Screen Companion Client")
    parser.add_argument(
        "--server",
        "-s",
        required=True,
        help="WebSocket server URL, e.g. ws://your-server:6315",
    )
    parser.add_argument(
        "--token",
        "-t",
        default="",
        help="Authentication token. Leave empty only if the server also allows no auth.",
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=float,
        default=10.0,
        help="周期素材上传间隔秒数（默认 10）；不影响按需截图的响应速度。",
    )
    parser.add_argument(
        "--quality",
        "-q",
        type=int,
        default=70,
        help="JPEG quality 1-100 (default: 70)",
    )
    parser.add_argument(
        "--client-id",
        default=f"client_{platform.node()}",
        help="Client identifier",
    )
    transfer_group = parser.add_mutually_exclusive_group()
    transfer_group.add_argument(
        "--binary",
        action="store_true",
        help="使用元数据 + raw JPEG（默认，推荐）",
    )
    transfer_group.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="使用 Base64 JSON 传输（体积更大，仅用于兼容排查）",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="显式启用周期截图上传；默认不周期上传，只在收到请求时采集。",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--screenshot-only", action="store_true", help="Only send screenshots")
    mode_group.add_argument("--video-only", action="store_true", help="Only send video clips")
    parser.add_argument("--video", action="store_true", help="Also upload a short video periodically")
    parser.add_argument("--video-duration", type=int, default=10, help="Video duration in seconds")
    parser.add_argument("--ffmpeg", dest="ffmpeg_path", default="", help="ffmpeg executable path")
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=30.0,
        help="原生 ping/pong 保活间隔秒数；0 表示关闭主动保活（默认 30）。",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = validate_config(args)
    except ClientConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    log.info("Starting remote screen client")
    log.info(f"  Server: {config.server_url}")
    log.info(f"  Client ID: {config.client_id}")
    log.info(f"  Quality: {config.image_quality}")
    for line in config.describe_modes():
        log.info(f"  {line}")

    try:
        asyncio.run(run_client(config))
    except KeyboardInterrupt:
        log.info("Client stopped")
    except IncompatibleServerError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
