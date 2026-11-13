# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Blender add-on that provides an MCP socket bridge-server.
"""

__all__ = (
    "register",
    "unregister",
)

import textwrap

import bpy  # pylint: disable=import-error
from bpy.props import (
    BoolProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)  # pylint: disable=import-error

from . import mcp_to_blender_server, project_settings

_PORT_MIN = 1024
_PORT_MAX = 65535

# Default seconds to wait after registration before auto-starting the server.
# Avoids adding work to Blender's startup sequence.
_AUTOSTART_DELAY = 1.0

# Store the CLI handle, only for correct register/unregister.
_cli_commands: list[object] = []

class _BlenderMCPPreferences(bpy.types.AddonPreferences):  # type: ignore[misc]
    bl_idname = __package__

    host: StringProperty(  # type: ignore[valid-type]
        name="Default Host",
        default=mcp_to_blender_server.DEFAULT_HOST,
    )
    port: IntProperty(  # type: ignore[valid-type]
        name="Default Port",
        default=mcp_to_blender_server.DEFAULT_PORT,
        min=_PORT_MIN,
        max=_PORT_MAX,
    )
    use_autostart: BoolProperty(  # type: ignore[valid-type]
        name="Auto Start",
        description=(
            "Allow automatic startup for saved projects that enable Auto Start This Project.\n"
            "New and unsaved projects always start stopped.\n"
            "(ignored in background mode)"
        ),
        default=True,
    )
    autostart_delay: FloatProperty(  # type: ignore[valid-type]
        name="Auto Start Delay",
        description=(
            "Seconds to wait after Blender starts before auto-starting the server.\n"
            "Avoids adding overhead to Blender's startup sequence"
        ),
        default=_AUTOSTART_DELAY,
        min=0.0,
        max=30.0,
        step=10,
        precision=1,
        subtype="TIME_ABSOLUTE",
    )

    def _update_use_log(self, _context: bpy.types.Context) -> None:
        mcp_to_blender_server.use_log = self.use_log

    use_log: BoolProperty(  # type: ignore[valid-type]
        name="Log",
        description="Print every tool request and response status to the terminal",
        default=False,
        update=_update_use_log,
    )

    def _update_timer_interval_active(self, _context: bpy.types.Context) -> None:
        # Cached on the server module because the timer callback may fire
        # many times a second, avoid slower preferences lookups.
        mcp_to_blender_server.timer_internal_vars_calc(active=self.timer_interval_active)

    timer_interval_active: FloatProperty(  # type: ignore[valid-type]
        name="Timer Interval",
        description="Seconds between queue polling ticks in interactive mode",
        default=0.25,
        min=0.05,
        max=5.0,
        step=1,
        precision=2,
        subtype='TIME_ABSOLUTE',
        update=_update_timer_interval_active,
    )

    def _update_timer_interval_idle(self, _context: bpy.types.Context) -> None:
        # Cached on the server module because the timer callback may fire
        # many times a second, avoid slower preferences lookups.
        mcp_to_blender_server.timer_internal_vars_calc(idle=self.timer_interval_idle)

    timer_interval_idle: FloatProperty(  # type: ignore[valid-type]
        name="Timer Interval Idle",
        description="Seconds between queue polling ticks while idle (no pending work)",
        default=1.0,
        min=0.1,
        max=10.0,
        step=10,
        precision=2,
        subtype='TIME_ABSOLUTE',
        update=_update_timer_interval_idle,
    )

    def _update_timer_interval_idle_delay(self, _context: bpy.types.Context) -> None:
        # Cached on the server module because the timer callback may fire
        # many times a second, avoid slower preferences lookups.
        mcp_to_blender_server.timer_internal_vars_calc(idle_delay=self.timer_interval_idle_delay)

    timer_interval_idle_delay: FloatProperty(  # type: ignore[valid-type]
        name="Idle Delay",
        description="Seconds of inactivity before switching to the idle polling interval",
        default=5.0,
        min=1.0,
        max=60.0,
        step=100,
        precision=1,
        subtype='TIME_ABSOLUTE',
        update=_update_timer_interval_idle_delay,
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text="Defaults for projects without MCP settings")
        layout.prop(self, "host")
        layout.prop(self, "port")
        layout.prop(self, "use_autostart")
        layout.prop(self, "autostart_delay")
        layout.prop(self, "timer_interval_active")
        layout.prop(self, "timer_interval_idle")
        layout.prop(self, "timer_interval_idle_delay")
        layout.prop(self, "use_log")

        layout.separator()
        _draw_project_settings(layout, context)


class _BLMCP_OT_server_start(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.server_start"
    bl_label = "Start MCP Bridge Server"
    bl_description = "Start the MCP socket bridge server that the MCP server can connect to"

    def execute(self, context: bpy.types.Context) -> set[str]:
        del context
        try:
            project_settings.start_server()
        except Exception as ex:  # pylint: disable=broad-exception-caught
            project_settings.session().error = str(ex)
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        endpoint = project_settings.session().endpoint
        self.report({"INFO"}, "MCP bridge started at {!s}".format(endpoint))
        return {"FINISHED"}


class _BLMCP_OT_server_stop(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.server_stop"
    bl_label = "Stop MCP Server"
    bl_description = "Stop the MCP Bridge Server"

    def execute(self, context: bpy.types.Context) -> set[str]:
        del context
        project_settings.stop_server()
        if not project_settings.session().conflict:
            project_settings.session().error = ""
        self.report({"INFO"}, "MCP bridge server stopped")
        return {"FINISHED"}


class _BLMCP_OT_apply_project_settings(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.apply_project_settings"
    bl_label = "Apply Settings to Project"
    bl_description = "Resolve conflicting scene settings using the values shown in this panel"

    def execute(self, context: bpy.types.Context) -> set[str]:
        del context
        try:
            project_settings.apply_settings()
        except (ValueError, RuntimeError) as ex:
            project_settings.session().error = str(ex)
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        return {'FINISHED'}


class _BLMCP_OT_copy_connection_settings(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.copy_connection_settings"
    bl_label = "Copy Connection Settings"
    bl_description = "Copy BLENDER_MCP_HOST and BLENDER_MCP_PORT for the MCP server process"

    def execute(self, context: bpy.types.Context) -> set[str]:
        context.window_manager.clipboard = project_settings.connection_settings()
        self.report({'INFO'}, "Copied bridge environment variables")
        return {'FINISHED'}


def _draw_project_settings(layout: bpy.types.UILayout, context: bpy.types.Context) -> None:
    state = project_settings.session()
    if not state.initialized:
        layout.label(text="Loading project settings...")
        return
    running = mcp_to_blender_server.is_running()
    status = "Running" if running else ("Error" if state.error else "Stopped")
    layout.label(text=status, icon='CHECKMARK' if running else ('ERROR' if state.error else 'PAUSE'))
    layout.label(text=bpy.path.basename(bpy.data.filepath) if bpy.data.filepath else "Unsaved project", icon='FILE_BLEND')
    if bpy.data.filepath:
        for line in textwrap.wrap(bpy.data.filepath, width=max(16, int((context.region.width - 70) / 7)),
                                  break_on_hyphens=False):
            layout.label(text=line)
    ui = context.window_manager.blmcp_session_settings
    endpoint_fields = layout.column()
    endpoint_fields.enabled = not running
    endpoint_fields.prop(ui, "host")
    endpoint_fields.prop(ui, "port")
    if running:
        layout.label(text="Stop the bridge to change its port")
    layout.prop(ui, "use_autostart")
    if state.endpoint:
        layout.label(text="Listening: {:s}:{:d}".format(*state.endpoint))
    if state.error:
        box = layout.box()
        box.alert = True
        for line in textwrap.wrap(state.error, width=max(24, int(context.region.width / 7) - 3)):
            box.label(text=line)
    if state.conflict:
        layout.operator("blmcp.apply_project_settings", icon='CHECKMARK')
    row = layout.row()
    row.enabled = running or not state.conflict
    row.operator("blmcp.server_stop" if running else "blmcp.server_start", icon='CANCEL' if running else 'PLAY')
    layout.operator("blmcp.copy_connection_settings", icon='COPYDOWN')
    if not bpy.data.filepath or bpy.data.is_dirty:
        layout.label(text="Save the .blend to keep project settings", icon='INFO')


class _BLMCP_PT_project(bpy.types.Panel):  # type: ignore[misc]
    bl_label = "MCP Project Ports"
    bl_idname = "BLMCP_PT_project"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MCP"

    def draw(self, context: bpy.types.Context) -> None:
        _draw_project_settings(self.layout, context)


def _cli_execute_handler(argv: list[str]) -> int:
    """
    Callback for the CLI: ``blender -c blender_mcp``.
    """
    if not bpy.app.online_access:
        print("Online access must be enabled; use --online-mode")
        return 1
    from .cli import cli_execute
    return cli_execute(argv)


_classes = (
    _BlenderMCPPreferences,
    _BLMCP_OT_server_start,
    _BLMCP_OT_server_stop,
    _BLMCP_OT_apply_project_settings,
    _BLMCP_OT_copy_connection_settings,
    _BLMCP_PT_project,
)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    _cli_commands.append(bpy.utils.register_cli_command("blender_mcp", _cli_execute_handler))
    project_settings.register()


def unregister() -> None:
    project_settings.unregister()
    for cmd in _cli_commands:
        bpy.utils.unregister_cli_command(cmd)
    _cli_commands.clear()

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
