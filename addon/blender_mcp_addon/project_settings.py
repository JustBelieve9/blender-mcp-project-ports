# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-FileCopyrightText: 2026 JustBelieve9
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Per-blend configuration and lifecycle of the interactive MCP bridge."""

__all__ = (
    "apply_settings",
    "connection_settings",
    "initialize",
    "register",
    "session",
    "start_server",
    "stop_server",
    "unregister",
)

import errno
import json
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import bpy  # pylint: disable=import-error
from bpy.app.handlers import persistent  # pylint: disable=import-error
from bpy.props import BoolProperty, IntProperty, PointerProperty, StringProperty  # pylint: disable=import-error

from . import execute_interactive, mcp_to_blender_server

_SCHEMA_VERSION = 1
_OFFLINE_ERROR = "Online access must be enabled in the system preferences"


def _persistent[Handler: Callable[..., None]](callback: Handler) -> Handler:
    """Preserve handler signatures through Blender's untyped decorator."""
    return cast(Handler, persistent(callback))


@dataclass(frozen=True)
class _Config:
    host: str = "127.0.0.1"
    port: int = 9876
    use_autostart: bool = False


@dataclass
class _Session:
    config: _Config = field(default_factory=_Config)
    initialized: bool = False
    configured: bool = False
    conflict: bool = False
    error: str = ""
    endpoint: tuple[str, int] | None = None
    generation: int = 0
    autostart_generation: int | None = None
    updating: bool = False


_state = _Session()


def session() -> _Session:
    """Return current session metadata, never saved into a blend file."""
    return _state


def _preferences() -> Any:
    return bpy.context.preferences.addons[__package__].preferences


def _local_scenes() -> list[Any]:
    return [scene for scene in bpy.data.scenes
            if scene.library is None and scene.override_library is None and scene.is_editable]


def _read_config(settings: Any) -> _Config:
    return _Config(settings.host.strip(), settings.port, settings.use_autostart)


def _validate(config: _Config) -> None:
    if not config.host:
        raise ValueError("Host cannot be empty")
    if not 1024 <= config.port <= 65535:
        raise ValueError("Port must be between 1024 and 65535")


def _sync_ui() -> None:
    _state.updating = True
    try:
        for wm in bpy.data.window_managers:
            ui = wm.blmcp_session_settings
            ui.host = _state.config.host
            ui.port = _state.config.port
            ui.use_autostart = _state.config.use_autostart
    finally:
        _state.updating = False


def _mirror() -> None:
    scenes = _local_scenes()
    if not scenes:
        raise RuntimeError("Add a local editable scene to store this project's MCP settings")
    for scene in scenes:
        saved = scene.blmcp_project_settings
        saved.schema_version = _SCHEMA_VERSION
        saved.host = _state.config.host
        saved.port = _state.config.port
        saved.use_autostart = _state.config.use_autostart
        saved.is_configured = True


def _set_error(ex: Exception) -> None:
    _state.error = str(ex)
    traceback.print_exception(ex, file=sys.stderr)


def _update_settings(self: Any, _context: Any) -> None:
    if _state.updating or not _state.initialized:
        return
    config = _read_config(self)
    if mcp_to_blender_server.is_running() and (config.host, config.port) != (
            _state.config.host, _state.config.port):
        _state.error = "Stop the bridge before changing Host or Port"
        _sync_ui()
        return
    _state.config = config
    _sync_ui()
    if not _state.conflict:
        try:
            apply_settings()
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _set_error(ex)


class _BLMCP_PG_project_settings(bpy.types.PropertyGroup):  # type: ignore[misc]
    schema_version: IntProperty(default=_SCHEMA_VERSION)  # type: ignore[valid-type]
    is_configured: BoolProperty(default=False)  # type: ignore[valid-type]
    host: StringProperty(default="127.0.0.1", options=set())  # type: ignore[valid-type]
    port: IntProperty(default=9876, min=1024, max=65535, options=set())  # type: ignore[valid-type]
    use_autostart: BoolProperty(default=False, options=set())  # type: ignore[valid-type]


class _BLMCP_PG_session_settings(bpy.types.PropertyGroup):  # type: ignore[misc]
    host: StringProperty(  # type: ignore[valid-type]
        name="Host", default="127.0.0.1", options={'SKIP_SAVE'}, update=_update_settings,
        description="Local address of this project's MCP bridge (normally 127.0.0.1)",
    )
    port: IntProperty(  # type: ignore[valid-type]
        name="Port", default=9876, min=1024, max=65535, options={'SKIP_SAVE'}, update=_update_settings,
        description="Unique TCP bridge port for this Blender process; save the blend file to keep it",
    )
    use_autostart: BoolProperty(  # type: ignore[valid-type]
        name="Auto Start This Project", default=False, options={'SKIP_SAVE'}, update=_update_settings,
        description="Start on opening this saved project, if Auto Start is also enabled in add-on preferences",
    )


def initialize() -> None:
    """Read one project's configuration without changing its saved data."""
    prefs = _preferences()
    config = _Config(prefs.host.strip(), prefs.port, False)
    configured = False
    conflict = False
    error = ""
    saved_configs = []
    for scene in sorted(_local_scenes(), key=lambda scene: scene.name):
        saved = scene.blmcp_project_settings
        if saved.is_configured:
            saved_configs.append(_read_config(saved))
            if saved.schema_version != _SCHEMA_VERSION:
                conflict = True
                error = "Unsupported settings version. Review the values and apply them to this project"
    if saved_configs:
        configured = True
        config = saved_configs[0]
        if any(item != config for item in saved_configs):
            conflict = True
            error = "Scenes have conflicting MCP settings. Choose the values and click Apply Settings to Project"
    try:
        _validate(config)
    except ValueError as ex:
        conflict = True
        error = str(ex)
    _state.config = config
    _state.configured = configured
    _state.conflict = conflict
    _state.error = error
    _state.initialized = True
    _sync_ui()


def apply_settings() -> None:
    """Explicitly persist the current values uniformly across local scenes."""
    _validate(_state.config)
    _mirror()
    _state.configured = True
    _state.conflict = False
    _state.error = ""


def _cancel_autostart() -> None:
    _state.autostart_generation = None
    if bpy.app.timers.is_registered(_autostart_timer):
        bpy.app.timers.unregister(_autostart_timer)


def start_server() -> None:
    """Start once, using this project's values, and roll back partial startup."""
    _cancel_autostart()
    if bpy.app.background:
        raise RuntimeError("Use --command blender_mcp --port PORT in background mode")
    if not bpy.app.online_access:
        raise RuntimeError(_OFFLINE_ERROR)
    if _state.conflict:
        raise RuntimeError("Resolve the settings conflict with Apply Settings to Project first")
    if mcp_to_blender_server.is_running():
        return
    apply_settings()
    prefs = _preferences()
    mcp_to_blender_server.timer_internal_vars_calc(
        active=prefs.timer_interval_active, idle=prefs.timer_interval_idle,
        idle_delay=prefs.timer_interval_idle_delay,
    )
    mcp_to_blender_server.use_log = prefs.use_log
    host, port = _state.config.host, _state.config.port
    try:
        mcp_to_blender_server.start(host, port)
        if not bpy.app.timers.is_registered(execute_interactive.run):
            bpy.app.timers.register(execute_interactive.run,
                                    first_interval=mcp_to_blender_server.TIMER_INTERVAL_ACTIVE, persistent=True)
    except Exception as ex:
        mcp_to_blender_server.stop()
        _state.endpoint = None
        if bpy.app.timers.is_registered(execute_interactive.run):
            bpy.app.timers.unregister(execute_interactive.run)
        _set_error(ex)
        if isinstance(ex, OSError) and (ex.errno == errno.EADDRINUSE or getattr(ex, "winerror", None) == 10048):
            message = "Port {:d} is already in use. Stop the other bridge or choose a different port".format(port)
        else:
            message = "Cannot start bridge at {:s}:{:d}: {:s}".format(host, port, str(ex))
        _state.error = message
        raise RuntimeError(message) from ex
    _state.endpoint = (host, port)
    _state.error = ""


def stop_server() -> None:
    """Stop this process only, including any pending automatic startup."""
    _cancel_autostart()
    mcp_to_blender_server.stop()
    if bpy.app.timers.is_registered(execute_interactive.run):
        bpy.app.timers.unregister(execute_interactive.run)
    _state.endpoint = None


def _autostart_allowed() -> bool:
    return bool(not bpy.app.background and bpy.data.filepath and _state.configured
                and not _state.conflict and _state.config.use_autostart and _preferences().use_autostart)


def _autostart_timer() -> None:
    generation = _state.autostart_generation
    _state.autostart_generation = None
    if generation != _state.generation or not _autostart_allowed() or mcp_to_blender_server.is_running():
        return
    try:
        start_server()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        _set_error(ex)


def _schedule_autostart() -> None:
    _cancel_autostart()
    if _autostart_allowed():
        _state.autostart_generation = _state.generation
        bpy.app.timers.register(_autostart_timer, first_interval=_preferences().autostart_delay)


@_persistent
def _load_pre(_filepath: str) -> None:
    stop_server()
    if bpy.app.timers.is_registered(_initialize_timer):
        bpy.app.timers.unregister(_initialize_timer)
    _state.generation += 1
    _state.initialized = False
    _state.configured = False
    _state.conflict = False
    _state.error = ""


@_persistent
def _load_post(_filepath: str) -> None:
    initialize()
    _schedule_autostart()


@_persistent
def _load_post_fail(_filepath: str) -> None:
    stop_server()
    initialize()
    _state.error = "File loading failed. The MCP bridge is stopped; review this project before starting it"


@_persistent
def _save_pre(_filepath: str) -> None:
    if _state.initialized and _state.configured and not _state.conflict:
        try:
            _mirror()
        except RuntimeError as ex:
            _set_error(ex)


@_persistent
def _restore_session_ui(_unused: Any) -> None:
    # Undo can restore WindowManager RNA; it must not retarget a live listener.
    if _state.initialized:
        _sync_ui()


def connection_settings() -> str:
    """Return bridge environment variables, not a client-specific config."""
    host, port = _state.endpoint or (_state.config.host, _state.config.port)
    return json.dumps({"BLENDER_MCP_HOST": host, "BLENDER_MCP_PORT": str(port)}, indent=2)


_classes = (_BLMCP_PG_project_settings, _BLMCP_PG_session_settings)
_handlers = (
    (bpy.app.handlers.load_pre, _load_pre),
    (bpy.app.handlers.load_post, _load_post),
    (bpy.app.handlers.load_post_fail, _load_post_fail),
    (bpy.app.handlers.save_pre, _save_pre),
    (bpy.app.handlers.undo_post, _restore_session_ui),
    (bpy.app.handlers.redo_post, _restore_session_ui),
)


def _initialize_timer() -> None:
    initialize()
    _schedule_autostart()


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.blmcp_project_settings = PointerProperty(type=_BLMCP_PG_project_settings)
    bpy.types.WindowManager.blmcp_session_settings = PointerProperty(type=_BLMCP_PG_session_settings,
                                                                   options={'SKIP_SAVE'})
    for handlers, callback in _handlers:
        if callback not in handlers:
            handlers.append(callback)
    # Add-on registration runs under RestrictData: defer access to scenes/WM.
    bpy.app.timers.register(_initialize_timer, first_interval=0.0)


def unregister() -> None:
    stop_server()
    if bpy.app.timers.is_registered(_initialize_timer):
        bpy.app.timers.unregister(_initialize_timer)
    for handlers, callback in _handlers:
        if callback in handlers:
            handlers.remove(callback)
    del bpy.types.WindowManager.blmcp_session_settings
    del bpy.types.Scene.blmcp_project_settings
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    _state.initialized = False
    _state.generation += 1
