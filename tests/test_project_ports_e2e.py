# SPDX-FileCopyrightText: 2026 JustBelieve9
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Two installed GUI instances, MCP SDK/stdio, persistence and lifecycle.

Run with an environment containing the local mcp package and the MCP SDK:
python tests/test_project_ports_e2e.py --blender BLENDER --zip ZIP --output EMPTY_DIR
All profiles, scenes, control files, screenshots and logs stay under output.
"""

__all__ = ()

import argparse
import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback

_MODULE = "bl_ext.user_default.mcp"


def _worker(config_path):
    import bpy
    import importlib

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    directory = Path(config["directory"])
    settings = importlib.import_module(_MODULE + ".project_settings")
    last_id = 0

    def tick():
        nonlocal last_id
        command_file = directory / "command.json"
        if command_file.exists():
            command = json.loads(command_file.read_text(encoding="utf-8"))
            if command["id"] != last_id:
                last_id = command["id"]
                namespace = {"bpy": bpy, "settings": settings, "result": {}}
                try:
                    exec(command["code"], namespace)
                    response = {"id": last_id, "result": namespace["result"]}
                except Exception:
                    response = {"id": last_id, "error": traceback.format_exc()}
                temporary = directory / "response.tmp"
                temporary.write_text(json.dumps(response), encoding="utf-8")
                temporary.replace(directory / "response.json")
        return 0.1

    def ready():
        if not settings.session().initialized:
            settings.initialize()
        if not config.get("reopen"):
            bpy.ops.wm.save_as_mainfile(filepath=str(directory / "unconfigured.blend"))
            ui = bpy.context.window_manager.blmcp_session_settings
            ui.host = "127.0.0.1"
            ui.port = config["port"]
            bpy.context.scene["e2e_project"] = config["name"]
            bpy.ops.wm.save_as_mainfile(filepath=str(directory / "project.blend"))
            settings.start_server()
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.spaces.active.show_region_ui = True
        bpy.app.timers.register(tick, first_interval=0.1, persistent=True)
        (directory / "ready.json").write_text(json.dumps({
            "pid": os.getpid(), "module": settings.__file__,
            "port": settings.session().config.port, "filepath": bpy.data.filepath,
        }), encoding="utf-8")

    bpy.app.timers.register(ready, first_interval=2.0)


class _Instance:
    def __init__(self, directory, blender, port, name):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.blender = blender
        self.port = port
        self.name = name
        self.proc = None
        self.log = None
        self.counter = 0
        profile = directory / "profile"
        temporary = directory / "temp"
        temporary.mkdir(exist_ok=True)
        self.env = {**os.environ, "BLENDER_USER_RESOURCES": str(profile),
                    "BLENDER_USER_CONFIG": str(profile / "config"),
                    "BLENDER_USER_SCRIPTS": str(profile / "scripts"),
                    "BLENDER_USER_EXTENSIONS": str(profile / "extensions"),
                    "TEMP": str(temporary), "TMP": str(temporary)}

    def install(self, archive):
        result = subprocess.run([self.blender, "--background", "--factory-startup", "--online-mode",
                                 "--command", "extension", "install-file", str(archive),
                                 "--repo", "user_default", "--enable"],
                                env=self.env, capture_output=True, timeout=60)
        (self.directory / "install.log").write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("Extension installation failed; see " + str(self.directory / "install.log"))
        prefs = subprocess.run([self.blender, "--background", "--python-exit-code", "1", "--python-expr",
                                "import bpy; bpy.context.preferences.view.show_splash = False; "
                                "bpy.ops.wm.save_userpref()"],
                               env=self.env, capture_output=True, timeout=30)
        (self.directory / "test-preferences.log").write_bytes(prefs.stdout + prefs.stderr)
        if prefs.returncode:
            raise RuntimeError("Could not disable the splash in the isolated test profile")

    def launch(self, reopen=False, offline=False):
        self.counter = 0
        for filename in ["command.json", "response.json", "ready.json"]:
            path = self.directory / filename
            if path.exists():
                path.unlink()
        config = {"directory": str(self.directory), "port": self.port, "name": self.name, "reopen": reopen}
        config_path = self.directory / "worker.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.log = (self.directory / ("gui-reopen.log" if reopen else "gui.log")).open("w", encoding="utf-8")
        args = [self.blender, "--offline-mode" if offline else "--online-mode", "--no-window-focus",
                "--window-geometry", "60", "60", "1150", "850"]
        if reopen:
            args.append(str(self.directory / "project.blend"))
        args.extend(["--python-exit-code", "1", "--python", str(Path(__file__).resolve()),
                     "--", "--worker", str(config_path)])
        self.proc = subprocess.Popen(args, env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 50
        while not (self.directory / "ready.json").exists():
            if self.proc.poll() is not None:
                raise RuntimeError("GUI exited early; see " + str(self.directory / "gui.log"))
            if time.monotonic() > deadline:
                raise TimeoutError("GUI did not become ready")
            time.sleep(0.1)
        ready = json.loads((self.directory / "ready.json").read_text(encoding="utf-8"))
        assert "user_default" in ready["module"] and "extensions" in ready["module"], ready
        assert ready["port"] == self.port, ready
        return ready

    def control(self, code):
        self.counter += 1
        command = {"id": self.counter, "code": code}
        temporary = self.directory / "command.tmp"
        temporary.write_text(json.dumps(command), encoding="utf-8")
        temporary.replace(self.directory / "command.json")
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            response_file = self.directory / "response.json"
            if response_file.exists():
                response = json.loads(response_file.read_text(encoding="utf-8"))
                if response["id"] == self.counter:
                    if "error" in response:
                        raise RuntimeError(response["error"])
                    return response["result"]
            if self.proc.poll() is not None:
                raise RuntimeError("GUI exited while waiting for control response")
            time.sleep(0.1)
        raise TimeoutError("GUI control timed out")

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.control("bpy.app.timers.register(lambda: bpy.ops.wm.quit_blender() and None, first_interval=0.3)")
                self.proc.wait(timeout=15)
            except (RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                self.proc.terminate()
                self.proc.wait(timeout=10)
        if self.log is not None:
            self.log.close()


def _free_ports():
    reservations = [socket.socket() for _ in range(2)]
    for item in reservations:
        item.bind(("127.0.0.1", 0))
    ports = [item.getsockname()[1] for item in reservations]
    for item in reservations:
        item.close()
    return ports


async def _connect(stack, port):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "BLENDER_MCP_HOST": "127.0.0.1", "BLENDER_MCP_PORT": str(port)}
    streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
        command=sys.executable, args=["-m", "blmcp"], env=env,
    )))
    client = await stack.enter_async_context(ClientSession(*streams))
    await client.initialize()
    return client


async def _execute(client, code, allow_error=False):
    response = await client.call_tool("execute_blender_code", {"code": code})
    if response.isError:
        if allow_error:
            return {"error": str(response.content)}
        raise RuntimeError(response.content)
    data = response.structuredContent
    if data is None:
        data = json.loads(next(item.text for item in response.content if item.type == "text"))
    if data.get("status") != "ok":
        raise RuntimeError(data)
    return data["result"]


async def _check_mcp(a, b, report):
    async with AsyncExitStack() as stack:
        ca = await _connect(stack, a.port)
        cb = await _connect(stack, b.port)
        code = ("import bpy, os\n"
                "marker = bpy.context.scene['e2e_project']\n"
                "bpy.context.scene['agent_' + marker] = marker\n"
                "result = {'pid': os.getpid(), 'filepath': bpy.data.filepath, "
                "'markers': [key for key in bpy.context.scene.keys() if key.startswith('agent_')]}\n")
        responses = await asyncio.gather(_execute(ca, code), _execute(cb, code))
        for instance, response in zip([a, b], responses):
            assert response["pid"] == instance.proc.pid, response
            assert Path(response["filepath"]) == instance.directory / "project.blend", response
            assert response["markers"] == ["agent_" + instance.name], response
        report["simultaneous_mcp_clients"] = responses
        a.control("settings.stop_server()")
        assert "error" in await _execute(ca, "result = {}", allow_error=True)
        assert await _execute(cb, "result = {'still_running': True}") == {"still_running": True}
        a.control("settings.start_server()")
        assert await _execute(ca, "result = {'restarted': True}") == {"restarted": True}
        report["stop_isolation_and_restart"] = "PASS"


def _screenshot(instance, filename):
    instance.control("\n".join([
        "for window in bpy.context.window_manager.windows:",
        "    for area in window.screen.areas:",
        "        if area.type == 'VIEW_3D':",
        "            area.spaces.active.show_region_ui = True",
        "            for region in area.regions:",
        "                if region.type == 'UI': region.active_panel_category = 'MCP'",
        "            area.tag_redraw()",
    ]))
    time.sleep(0.8)
    instance.control("bpy.ops.screen.screenshot(filepath={!r})".format(str(instance.directory / filename)))


def _check_cli(instance):
    port = _free_ports()[0]
    blend = instance.directory / "project.blend"
    before = hashlib.sha256(blend.read_bytes()).hexdigest()
    with (instance.directory / "background-cli.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen([instance.blender, "--background", "--online-mode", str(blend),
                                 "--command", "blender_mcp", "--host", "127.0.0.1", "--port", str(port)],
                                env=instance.env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while True:
                if proc.poll() is not None:
                    raise RuntimeError("Background CLI exited before accepting connections")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Background CLI did not start")
                    time.sleep(0.1)

            async def query():
                async with AsyncExitStack() as stack:
                    client = await _connect(stack, port)
                    response = await _execute(client, "import bpy, os\nresult = {'pid': os.getpid(), "
                                              "'saved_port': bpy.context.scene.blmcp_project_settings.port}")
                    assert response == {"pid": proc.pid, "saved_port": instance.port}, response
                    await _execute(client, "import importlib\nimportlib.import_module({!r}).stop()\n"
                                   "result = {{'stopped': True}}".format(_MODULE + ".mcp_to_blender_server"))
            asyncio.run(query())
            assert proc.wait(timeout=15) == 0
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
    assert hashlib.sha256(blend.read_bytes()).hexdigest() == before


def main(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Use an empty output directory to keep test artifacts isolated")
    ports = _free_ports()
    a = _Instance(output / "a", args.blender, ports[0], "a")
    b = _Instance(output / "b", args.blender, ports[1], "b")
    report = {"ports": ports}
    try:
        for instance in [a, b]:
            instance.install(Path(args.zip).resolve())
            report[instance.name] = instance.launch()
        asyncio.run(_check_mcp(a, b, report))
        _screenshot(a, "panel-running.png")

        b.control("settings.stop_server()\nbpy.context.window_manager.blmcp_session_settings.port = " + str(a.port))
        failure = b.control("try:\n    bpy.ops.blmcp.server_start()\nexcept RuntimeError:\n    pass\n"
                            "result = {'error': settings.session().error, 'endpoint': settings.session().endpoint}")
        assert failure["error"] and failure["endpoint"] is None, failure
        _screenshot(b, "panel-occupied.png")
        b.control("bpy.context.window_manager.blmcp_session_settings.port = {:d}\nsettings.start_server()".format(b.port))
        report["occupied_port"] = failure

        windows = b.control("""original = bpy.context.window_manager.windows[0]
with bpy.context.temp_override(window=original):
    bpy.ops.wm.window_new()
windows = list(bpy.context.window_manager.windows)
result = {'windows': len(windows), 'port': bpy.context.window_manager.blmcp_session_settings.port}
with bpy.context.temp_override(window=windows[-1]):
    bpy.ops.wm.window_close()
""")
        assert windows["windows"] == 2 and windows["port"] == b.port, windows
        report["multiple_windows_one_port"] = "PASS"

        undo = a.control("""window = bpy.context.window_manager.windows[0]
area = next(area for area in window.screen.areas if area.type == 'VIEW_3D')
with bpy.context.temp_override(window=window, area=area):
    bpy.ops.ed.undo_push(message='Before new scene')
    bpy.ops.scene.new(type='NEW')
    bpy.ops.ed.undo_push(message='After new scene')
    bpy.ops.ed.undo()
    bpy.ops.ed.redo()
result = {'port': bpy.context.window_manager.blmcp_session_settings.port,
          'endpoint': settings.session().endpoint}
""")
        assert undo["port"] == a.port and undo["endpoint"][1] == a.port, undo
        report["scene_undo_redo"] = "PASS"

        a.control("bpy.context.window_manager.blmcp_session_settings.use_autostart = True\n"
                  "bpy.ops.wm.save_as_mainfile(filepath={!r})".format(str(a.directory / "project.blend")))
        a.close()
        report["reopened"] = a.launch(reopen=True)
        time.sleep(1.2)
        assert a.control("result = {'endpoint': settings.session().endpoint}")["endpoint"][1] == a.port
        report["full_restart_project_autostart"] = "PASS"

        a.control("settings.stop_server()\nsettings._schedule_autostart()\nsettings.stop_server()")
        time.sleep(1.3)
        assert a.control("result = {'endpoint': settings.session().endpoint}")["endpoint"] is None
        report["stop_cancels_delayed_autostart"] = "PASS"

        a.control("settings._schedule_autostart()\nbpy.ops.wm.open_mainfile(filepath={!r})".format(
            str(a.directory / "unconfigured.blend")))
        time.sleep(1.3)
        loaded = a.control("result = {'configured': settings.session().configured, 'endpoint': settings.session().endpoint}")
        assert loaded == {"configured": False, "endpoint": None}, loaded
        report["load_cancels_old_connection_and_autostart"] = "PASS"

        a.close()
        a.launch(reopen=True, offline=True)
        time.sleep(1.2)
        offline = a.control("result = {'error': settings.session().error, 'endpoint': settings.session().endpoint}")
        assert offline["endpoint"] is None and "Online access" in offline["error"], offline
        report["offline_access"] = "PASS"
        _check_cli(a)
        report["background_cli_explicit_port_and_no_file_write"] = "PASS"
        report["status"] = "PASS"
    except Exception as ex:
        report["status"] = "FAIL"
        report["error"] = repr(ex)
        raise
    finally:
        a.close()
        b.close()
        (output / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument("--blender")
    parser.add_argument("--zip")
    parser.add_argument("--output")
    arguments = parser.parse_args(argv)
    if arguments.worker:
        _worker(arguments.worker)
    else:
        main(arguments)
