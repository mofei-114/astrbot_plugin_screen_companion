# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Plain

from astrbot_plugin_screen_companion import main
from astrbot_plugin_screen_companion.core.remote_receiver import RemoteScreenshotError


class ToolContextTests(unittest.IsolatedAsyncioTestCase):
    def test_get_tool_event_supports_wrapped_and_direct_contexts(self) -> None:
        event = SimpleNamespace(unified_msg_origin="napcat:FriendMessage:2306087691")
        agent_context = SimpleNamespace(event=event)
        wrapper = SimpleNamespace(context=agent_context)

        self.assertIs(main._get_tool_event(wrapper), event)
        self.assertIs(main._get_tool_event(agent_context), event)

    def test_resolve_tool_event_uses_task_local_hook_fallback(self) -> None:
        event = SimpleNamespace(unified_msg_origin="napcat:FriendMessage:2306087691")
        token = main._screen_companion_current_tool_event.set(event)
        try:
            self.assertIs(main._resolve_tool_event(SimpleNamespace()), event)
        finally:
            main._screen_companion_current_tool_event.reset(token)
    async def test_screen_peek_passes_complete_context_to_permission_check(self) -> None:
        wrapper = SimpleNamespace(context=SimpleNamespace(event=object()))
        received_contexts = []

        async def deny(_plugin, context, *, tool_name):
            received_contexts.append((context, tool_name))
            return False, "denied"

        with (
            patch.object(main, "_screen_companion_tool_plugin", object()),
            patch.object(main, "_ensure_tool_admin_permission", side_effect=deny),
        ):
            result = await main.ScreenPeekTool().call(wrapper)

        self.assertEqual(result, "denied")
        self.assertEqual(received_contexts, [(wrapper, "screen_peek")])

    async def test_screen_peek_keeps_direct_agent_context_intact(self) -> None:
        agent_context = SimpleNamespace(event=object(), context=object())
        received_contexts = []

        async def deny(_plugin, context, *, tool_name):
            received_contexts.append((context, tool_name))
            return False, "denied"

        with (
            patch.object(main, "_screen_companion_tool_plugin", object()),
            patch.object(main, "_ensure_tool_admin_permission", side_effect=deny),
        ):
            result = await main.ScreenPeekTool().call(agent_context)

        self.assertEqual(result, "denied")
        self.assertEqual(received_contexts, [(agent_context, "screen_peek")])

    async def test_usage_tool_passes_complete_context_to_permission_check(self) -> None:
        wrapper = SimpleNamespace(context=SimpleNamespace(event=object()))
        received_contexts = []

        async def deny(_plugin, context, *, tool_name):
            received_contexts.append((context, tool_name))
            return False, "denied"

        with (
            patch.object(main, "_screen_companion_tool_plugin", object()),
            patch.object(main, "_ensure_tool_admin_permission", side_effect=deny),
        ):
            result = await main.ScreenUsageContextTool().call(wrapper)

        self.assertEqual(result, "denied")
        self.assertEqual(received_contexts, [(wrapper, "screen_usage_context")])

    async def test_tool_hooks_bind_and_clear_screen_event(self) -> None:
        event = SimpleNamespace(unified_msg_origin="napcat:FriendMessage:2306087691")
        tool = SimpleNamespace(name="screen_peek")
        token = main._screen_companion_current_tool_event.set(None)
        try:
            await main.ScreenCompanion.bind_screen_tool_event(
                object(), event, tool, {"question": "看看屏幕"}
            )
            self.assertIs(main._screen_companion_current_tool_event.get(), event)

            await main.ScreenCompanion.clear_screen_tool_event(
                object(), event, tool, {"question": "看看屏幕"}, "result"
            )
            self.assertIsNone(main._screen_companion_current_tool_event.get())
        finally:
            main._screen_companion_current_tool_event.reset(token)


class ScreenPeekRemoteCaptureTests(unittest.IsolatedAsyncioTestCase):
    """screen_peek 仅在远程图像模式要求新帧，并把远程错误呈现为工具失败。"""

    def _make_plugin(self, *, remote_mode: bool, recording: bool, result=None, error=None):
        plugin = SimpleNamespace(
            remote_mode=remote_mode,
            screen_recognition_mode=recording,
            _get_runtime_flag=lambda name, default=False: bool(
                getattr(SimpleNamespace(remote_mode=remote_mode), name, default)
            ),
            _use_screen_recording_mode=lambda: recording,
        )
        calls: list = []

        async def capture_recognition_context(**kwargs):
            calls.append(kwargs)
            if error is not None:
                raise error
            return {
                "media_kind": "image",
                "media_bytes": b"\xff\xd8x\xff\xd9",
                "active_window_title": "Editor",
            }

        plugin._capture_recognition_context = capture_recognition_context
        plugin._analyze_screen = AsyncMock(
            return_value=[Plain("看到了编辑器")] if result is None else result
        )
        return plugin, calls

    async def test_remote_image_mode_requests_fresh_frame(self) -> None:
        plugin, calls = self._make_plugin(remote_mode=True, recording=False)
        wrapper = SimpleNamespace(context=SimpleNamespace(event=object()))

        with (
            patch.object(main, "_screen_companion_tool_plugin", plugin),
            patch.object(
                main, "_ensure_tool_admin_permission", AsyncMock(return_value=(True, ""))
            ),
        ):
            result = await main.ScreenPeekTool().call(wrapper, question="看看")

        self.assertEqual("看到了编辑器", result)
        self.assertEqual([{"force_fresh_capture": True}], calls)
        plugin._analyze_screen.assert_awaited_once()

    async def test_local_mode_keeps_default_capture_parameters(self) -> None:
        plugin, calls = self._make_plugin(remote_mode=False, recording=False)
        wrapper = SimpleNamespace(context=SimpleNamespace(event=object()))

        with (
            patch.object(main, "_screen_companion_tool_plugin", plugin),
            patch.object(
                main, "_ensure_tool_admin_permission", AsyncMock(return_value=(True, ""))
            ),
        ):
            await main.ScreenPeekTool().call(wrapper)

        # 本地/共享目录模式保留原默认，不借远程改造改变本地选择。
        self.assertEqual([{"force_fresh_capture": False}], calls)

    async def test_remote_recording_mode_does_not_force_fresh_image(self) -> None:
        plugin, calls = self._make_plugin(remote_mode=True, recording=True)
        wrapper = SimpleNamespace(context=SimpleNamespace(event=object()))

        with (
            patch.object(main, "_screen_companion_tool_plugin", plugin),
            patch.object(
                main, "_ensure_tool_admin_permission", AsyncMock(return_value=(True, ""))
            ),
        ):
            await main.ScreenPeekTool().call(wrapper)

        self.assertEqual([{"force_fresh_capture": False}], calls)

    async def test_remote_capture_error_is_surfaced_without_vision_call(self) -> None:
        error = RemoteScreenshotError(
            "timeout", "远程截图超时，请检查网络或客户端截图权限。"
        )
        plugin, _calls = self._make_plugin(
            remote_mode=True, recording=False, error=error
        )
        wrapper = SimpleNamespace(context=SimpleNamespace(event=object()))

        with (
            patch.object(main, "_screen_companion_tool_plugin", plugin),
            patch.object(
                main, "_ensure_tool_admin_permission", AsyncMock(return_value=(True, ""))
            ),
        ):
            result = await main.ScreenPeekTool().call(wrapper)

        self.assertIn(error.public_message, result)
        # 采集失败不进入视觉分析。
        plugin._analyze_screen.assert_not_called()

    async def test_denied_permission_never_requests_capture(self) -> None:
        plugin, calls = self._make_plugin(remote_mode=True, recording=False)
        wrapper = SimpleNamespace(context=SimpleNamespace(event=None))

        with (
            patch.object(main, "_screen_companion_tool_plugin", plugin),
            patch.object(
                main,
                "_ensure_tool_admin_permission",
                AsyncMock(return_value=(False, "denied")),
            ),
        ):
            result = await main.ScreenPeekTool().call(wrapper)

        self.assertEqual("denied", result)
        self.assertEqual([], calls)
        plugin._analyze_screen.assert_not_called()

    async def test_missing_tool_context_never_requests_capture(self) -> None:
        plugin, calls = self._make_plugin(remote_mode=True, recording=False)
        wrapper = SimpleNamespace()
        token = main._screen_companion_current_tool_event.set(None)
        try:
            with patch.object(main, "_screen_companion_tool_plugin", plugin):
                result = await main.ScreenPeekTool().call(wrapper)
        finally:
            main._screen_companion_current_tool_event.reset(token)

        self.assertIn("权限不足", result)
        self.assertEqual([], calls)

    async def test_work_collaboration_context_never_captures(self) -> None:
        plugin = ScreenCompanionRemoteEntryTests._make_work_plugin()
        # 只读缓存接口不得依赖任何采集入口：没有该属性本身即是约束。
        self.assertFalse(hasattr(plugin, "_capture_recognition_context"))
        api = main.ScreenCompanionExtensionAPI(plugin)

        result = await api.get_work_collaboration_context(user_id="10001")

        self.assertTrue(result["available"])
        plugin._analyze_screen.assert_not_called()


class ScreenCompanionRemoteEntryTests(unittest.IsolatedAsyncioTestCase):
    """自然语言与 /kp 入口的远程错误呈现。"""

    @staticmethod
    def _make_work_plugin() -> SimpleNamespace:
        now = datetime.datetime.now().timestamp()
        return SimpleNamespace(
            mask_activity_window_titles=False,
            enable_background_activity_tracking=True,
            is_running=False,
            auto_tasks={},
            _get_active_window_info=lambda: ("Editor", None),
            _identify_scene=lambda _window: "编程",
            _build_current_activity_snapshot=lambda: {
                "type": "工作",
                "scene": "编程",
                "window": "Editor",
                "app_name": "Editor",
                "resource_label": "Editor",
                "duration": 60,
                "end_time": now,
            },
            _build_activity_record_meta=lambda **kwargs: dict(kwargs),
            _get_recent_screen_analysis_traces=lambda limit=8: [],
            _analyze_screen=AsyncMock(),
        )

    async def test_natural_language_failure_reports_once_and_stops_event(self) -> None:
        error = RemoteScreenshotError(
            "no_client", "远程客户端未连接，请启动客户端后重试。"
        )
        captured_replies: list = []
        stop_calls: list = []

        class FakeEvent:
            message_str = "帮我看看屏幕上写了什么"
            unified_msg_origin = "napcat:FriendMessage:10001"

            def stop_event(self) -> None:
                stop_calls.append(True)

            def plain_result(self, text):
                captured_replies.append(text)
                return text

        plugin = main.ScreenCompanion.__new__(main.ScreenCompanion)
        plugin.enable_natural_language_screen_assist = True
        plugin.debug = False
        plugin._screen_assist_cooldowns = {}
        plugin._allow_implicit_screen_skill_trigger = lambda event, text: False
        plugin._extract_screen_assist_prompt = lambda text, allow_implicit: text
        plugin._is_private_message_event = lambda event: True
        plugin._ensure_admin_permission = AsyncMock(return_value=True)
        plugin._invoke_screen_skill = AsyncMock(side_effect=error)
        plugin._split_message = lambda text: [text]

        event = FakeEvent()
        results = [
            item
            async for item in plugin.on_natural_language_screen_assist(event)
        ]

        self.assertEqual(1, len(results))
        self.assertIn(error.public_message, results[0])
        self.assertEqual([True], stop_calls)
        # 失败时冷却已消费，但不应产生成功轨迹记录。
        plugin._invoke_screen_skill.assert_awaited_once()

    async def test_natural_language_not_triggered_never_captures(self) -> None:
        class FakeEvent:
            message_str = "今天天气不错"
            unified_msg_origin = "napcat:FriendMessage:10001"

        plugin = main.ScreenCompanion.__new__(main.ScreenCompanion)
        plugin.enable_natural_language_screen_assist = True
        plugin.debug = False
        plugin._allow_implicit_screen_skill_trigger = lambda event, text: False
        plugin._extract_screen_assist_prompt = lambda text, allow_implicit: ""
        plugin._invoke_screen_skill = AsyncMock()

        results = [
            item
            async for item in plugin.on_natural_language_screen_assist(FakeEvent())
        ]

        self.assertEqual([], results)
        plugin._invoke_screen_skill.assert_not_called()

    async def test_natural_language_disabled_never_captures(self) -> None:
        class FakeEvent:
            message_str = "帮我看看屏幕"
            unified_msg_origin = "napcat:FriendMessage:10001"

        plugin = main.ScreenCompanion.__new__(main.ScreenCompanion)
        plugin.enable_natural_language_screen_assist = False
        plugin._invoke_screen_skill = AsyncMock()

        results = [
            item
            async for item in plugin.on_natural_language_screen_assist(FakeEvent())
        ]

        self.assertEqual([], results)
        plugin._invoke_screen_skill.assert_not_called()

    async def test_group_message_never_captures(self) -> None:
        class FakeEvent:
            message_str = "帮我看看屏幕上写了什么"
            unified_msg_origin = "napcat:GroupMessage:999"

        plugin = main.ScreenCompanion.__new__(main.ScreenCompanion)
        plugin.enable_natural_language_screen_assist = True
        plugin.debug = False
        plugin._allow_implicit_screen_skill_trigger = lambda event, text: False
        plugin._extract_screen_assist_prompt = lambda text, allow_implicit: text
        plugin._is_private_message_event = lambda event: False
        plugin._is_group_message_event = lambda event: True
        plugin._get_event_sender_id = lambda event: "999"
        plugin._invoke_screen_skill = AsyncMock()

        results = [
            item
            async for item in plugin.on_natural_language_screen_assist(FakeEvent())
        ]

        self.assertEqual([], results)
        plugin._invoke_screen_skill.assert_not_called()


class SharedActivityExtensionTests(unittest.TestCase):
    def test_external_watch_is_recorded_without_overwriting_screen_activity(self) -> None:
        plugin = main.ScreenCompanion.__new__(main.ScreenCompanion)
        plugin._external_shared_activities = {}
        plugin.current_activity = "工作:编程:VS Code"
        plugin.activity_start_time = 1.0
        captured = []
        plugin._build_activity_record_meta = lambda **kwargs: dict(kwargs)
        plugin._append_activity_record = lambda **kwargs: captured.append(kwargs) or True
        api = main.ScreenCompanionExtensionAPI(plugin)

        item = api.notify_shared_activity_started(
            "together:room",
            user_id="10001",
            kind="shared_watch",
            label="正在一起看《测试影片》",
            source_plugin="astrbot_plugin_together_companion",
        )
        item["started_at"] -= 10
        plugin._external_shared_activities["together:room"] = item
        ended = api.notify_shared_activity_ended("together:room")

        self.assertTrue(ended)
        self.assertEqual("工作:编程:VS Code", plugin.current_activity)
        self.assertEqual("摸鱼:视频:正在一起看《测试影片》", captured[0]["activity"])
        self.assertEqual("astrbot_plugin_together_companion", captured[0]["activity_meta"]["capture_source"])

    def test_external_work_is_recorded_as_office_work(self) -> None:
        plugin = main.ScreenCompanion.__new__(main.ScreenCompanion)
        plugin._external_shared_activities = {}
        captured = []
        plugin._build_activity_record_meta = lambda **kwargs: dict(kwargs)
        plugin._append_activity_record = lambda **kwargs: captured.append(kwargs) or True
        api = main.ScreenCompanionExtensionAPI(plugin)

        item = api.notify_shared_activity_started(
            "together:work-room",
            kind="shared_work",
            source_plugin="astrbot_plugin_together_companion",
        )
        item["started_at"] -= 10
        plugin._external_shared_activities["together:work-room"] = item

        self.assertTrue(api.notify_shared_activity_ended("together:work-room"))
        self.assertEqual("工作:办公:一起工作", captured[0]["activity"])
        self.assertEqual("工作", captured[0]["activity_meta"]["activity_type"])
        self.assertEqual("办公", captured[0]["activity_meta"]["scene"])


class WorkCollaborationContextTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_plugin(*, privacy_masked: bool = False) -> SimpleNamespace:
        window = "Secret roadmap - Visual Studio Code"
        now = datetime.datetime.now().isoformat()
        snapshot = {
            "type": "工作",
            "scene": "编程",
            "window": window,
            "app_name": "Visual Studio Code",
            "resource_label": "Secret roadmap",
            "duration": 125,
            "end_time": datetime.datetime.now().timestamp(),
        }
        trace = {
            "timestamp": now,
            "status": "ok",
            "scene": "编程",
            "active_window_title": window,
            "display_title": "Secret roadmap",
            "fact_summary": "正在 Secret roadmap 中整理下一版本的实现步骤",
            "frame_labels": ["不应暴露"],
            "image": "not-returned",
        }

        def build_meta(*, activity_type, scene, window):
            return {
                "type": activity_type,
                "scene": scene,
                "window": window,
                "app_name": "Visual Studio Code",
                "resource_label": "Secret roadmap",
            }

        return SimpleNamespace(
            mask_activity_window_titles=privacy_masked,
            enable_background_activity_tracking=True,
            is_running=False,
            auto_tasks={},
            _get_active_window_info=lambda: (window, None),
            _identify_scene=lambda _window: "编程",
            _build_current_activity_snapshot=lambda: dict(snapshot),
            _build_activity_record_meta=build_meta,
            _get_recent_screen_analysis_traces=lambda limit=8: [dict(trace)][:limit],
        )

    async def test_work_context_uses_cached_structured_state(self) -> None:
        api = main.ScreenCompanionExtensionAPI(self._make_plugin())

        result = await api.get_work_collaboration_context(user_id="10001")

        self.assertTrue(result["available"])
        self.assertTrue(result["context_available"])
        self.assertTrue(result["tracking_enabled"])
        self.assertEqual("工作", result["current"]["type"])
        self.assertEqual("编程", result["current"]["scene"])
        self.assertEqual("Visual Studio Code", result["current"]["app_name"])
        self.assertEqual(125, result["current"]["duration_seconds"])
        self.assertIn("下一版本", result["observation"]["summary"])
        self.assertGreater(result["captured_at"], 0)
        self.assertNotIn("image", repr(result))
        self.assertNotIn("frame_labels", repr(result))
        self.assertTrue(api.get_capabilities()["work_collaboration_context"])
        self.assertTrue(api.get_capabilities()["shared_work"])

    async def test_work_context_masks_window_titles_and_summary(self) -> None:
        api = main.ScreenCompanionExtensionAPI(
            self._make_plugin(privacy_masked=True)
        )

        result = await api.get_work_collaboration_context()

        self.assertTrue(result["privacy_masked"])
        self.assertEqual("已脱敏 · Visual Studio Code", result["current"]["window"])
        self.assertEqual("Visual Studio Code", result["current"]["resource_label"])
        self.assertNotIn("Secret roadmap", repr(result))

    async def test_work_context_failure_does_not_escape_to_caller(self) -> None:
        def fail_window_read():
            raise RuntimeError("window access failed")

        plugin = self._make_plugin()
        plugin._get_active_window_info = fail_window_read
        api = main.ScreenCompanionExtensionAPI(plugin)

        result = await api.get_work_collaboration_context()

        self.assertTrue(result["available"])
        self.assertFalse(result["context_available"])
        self.assertEqual({}, result["current"])
        self.assertEqual({}, result["observation"])


if __name__ == "__main__":
    unittest.main()
