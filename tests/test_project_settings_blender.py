# SPDX-FileCopyrightText: 2026 JustBelieve9
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Run in an isolated Blender: blender -b --factory-startup -P THIS -- SCRATCH."""

__all__ = ()

import json
from pathlib import Path
import socket
import sys
import unittest

import addon_utils
import bpy

_REPO = Path(__file__).resolve().parents[1]
_SCRATCH = Path(sys.argv[sys.argv.index("--") + 1])
sys.path.insert(0, str(_REPO / "addon"))
addon_utils.enable("blender_mcp_addon", default_set=True)
from blender_mcp_addon import project_settings as settings
from blender_mcp_addon import mcp_to_blender_server as bridge


class TestProjectSettings(unittest.TestCase):
    def setUp(self):
        settings.stop_server()
        self.prefs = bpy.context.preferences.addons["blender_mcp_addon"].preferences
        self.prefs.host = "127.0.0.1"
        self.prefs.port = 9876
        for scene in list(bpy.data.scenes):
            if scene.library is None:
                scene.blmcp_project_settings.is_configured = False
        settings.initialize()

    def test_new_file_uses_defaults_without_configuring(self):
        self.assertFalse(settings.session().configured)
        self.assertFalse(settings.session().config.use_autostart)
        self.assertFalse(bridge.is_running())

    def test_port_mirrored_and_preferences_unchanged(self):
        second = bpy.data.scenes.new("Second")
        bpy.context.window_manager.blmcp_session_settings.port = 12001
        self.assertTrue(all(s.blmcp_project_settings.port == 12001 for s in settings._local_scenes()))
        self.assertEqual(self.prefs.port, 9876)
        bpy.data.scenes.remove(second)

    def test_scene_switch_and_delete_original(self):
        bpy.context.window_manager.blmcp_session_settings.port = 12002
        original = bpy.context.scene
        second = bpy.data.scenes.new("Replacement")
        bpy.context.window.scene = second
        bpy.data.scenes.remove(original)
        self.assertEqual(settings.session().config.port, 12002)
        path = _SCRATCH / "replacement.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(path))
        bpy.ops.wm.open_mainfile(filepath=str(path))
        self.assertEqual(settings.session().config.port, 12002)
        self.assertEqual(bpy.context.scene.blmcp_project_settings.port, 12002)

    def test_save_reopen_and_save_as(self):
        bpy.context.window_manager.blmcp_session_settings.port = 12003
        bpy.context.window_manager.blmcp_session_settings.use_autostart = True
        path = _SCRATCH / "persist.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(path))
        bpy.ops.wm.open_mainfile(filepath=str(path))
        self.assertEqual(settings.session().config.port, 12003)
        self.assertTrue(settings.session().config.use_autostart)
        copy_path = _SCRATCH / "persist_copy.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(copy_path))
        self.assertEqual(settings.session().config.port, 12003)

    def test_conflict_requires_explicit_apply(self):
        bpy.context.window_manager.blmcp_session_settings.port = 12004
        second = bpy.data.scenes.new("Conflict")
        second.blmcp_project_settings.is_configured = True
        second.blmcp_project_settings.port = 12005
        settings.initialize()
        self.assertTrue(settings.session().conflict)
        bpy.context.window_manager.blmcp_session_settings.port = 12006
        self.assertEqual(second.blmcp_project_settings.port, 12005)
        settings.apply_settings()
        self.assertFalse(settings.session().conflict)
        self.assertTrue(all(s.blmcp_project_settings.port == 12006 for s in settings._local_scenes()))
        bpy.data.scenes.remove(second)

    def test_unsupported_schema_is_not_autostarted(self):
        saved = bpy.context.scene.blmcp_project_settings
        saved.is_configured = True
        saved.schema_version = 99
        settings.initialize()
        self.assertTrue(settings.session().conflict)
        settings.apply_settings()
        self.assertEqual(saved.schema_version, 1)

    def test_linked_scene_not_used_or_written(self):
        library_scene = bpy.data.scenes.new("ExternalScene")
        library_scene.blmcp_project_settings.is_configured = True
        library_scene.blmcp_project_settings.port = 12007
        library_path = _SCRATCH / "library.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(library_path), copy=True)
        bpy.data.scenes.remove(library_scene)
        with bpy.data.libraries.load(str(library_path), link=True) as (_, target):
            target.scenes = ["ExternalScene"]
        linked = target.scenes[0]
        bpy.context.window_manager.blmcp_session_settings.port = 12008
        self.assertEqual(linked.blmcp_project_settings.port, 12007)
        self.assertFalse(settings.session().conflict)

    def test_load_pre_stops_listener_and_timer(self):
        bridge.start("127.0.0.1", 0)
        settings._state.autostart_generation = 1
        bpy.app.timers.register(settings._autostart_timer, first_interval=20)
        settings._load_pre("")
        self.assertFalse(bridge.is_running())
        self.assertFalse(bpy.app.timers.is_registered(settings._autostart_timer))
        settings._load_post("")
        self.assertFalse(bridge.is_running())

    def test_failed_load_is_stopped(self):
        bridge.start("127.0.0.1", 0)
        settings._load_pre("")
        settings._load_post_fail("")
        self.assertFalse(bridge.is_running())
        self.assertIn("loading failed", settings.session().error)

    def test_stop_cancels_autostart(self):
        settings._state.autostart_generation = settings.session().generation
        bpy.app.timers.register(settings._autostart_timer, first_interval=20)
        settings.stop_server()
        self.assertIsNone(settings.session().autostart_generation)
        self.assertFalse(bpy.app.timers.is_registered(settings._autostart_timer))

    def test_occupied_port_does_not_start_listener(self):
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with self.assertRaises(OSError):
                bridge.start("127.0.0.1", occupied.getsockname()[1])
            self.assertFalse(bridge.is_running())

    def test_running_port_edit_is_rejected(self):
        bpy.context.window_manager.blmcp_session_settings.port = 12009
        bridge.start("127.0.0.1", 0)
        bpy.context.window_manager.blmcp_session_settings.port = 12010
        self.assertEqual(settings.session().config.port, 12009)
        self.assertEqual(bpy.context.window_manager.blmcp_session_settings.port, 12009)

    def test_invalid_host_reports_error_without_mirroring(self):
        bpy.context.window_manager.blmcp_session_settings.host = ""
        self.assertIn("Host cannot be empty", settings.session().error)
        self.assertFalse(settings.session().configured)

    def test_connection_json(self):
        bpy.context.window_manager.blmcp_session_settings.port = 12011
        env = json.loads(settings.connection_settings())
        self.assertEqual(env, {"BLENDER_MCP_HOST": "127.0.0.1", "BLENDER_MCP_PORT": "12011"})

    def test_invalid_port_rejected_by_service(self):
        with self.assertRaises(ValueError):
            settings._validate(settings._Config("127.0.0.1", 0))

    def test_unregister_register_has_one_handler(self):
        import blender_mcp_addon
        blender_mcp_addon.unregister()
        self.assertFalse(hasattr(bpy.types.Scene, "blmcp_project_settings"))
        blender_mcp_addon.register()
        self.assertEqual(bpy.app.handlers.load_pre.count(settings._load_pre), 1)
        self.assertFalse(bridge.is_running())


if __name__ == "__main__":
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestProjectSettings)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError("Project settings tests failed")
