# Blender Lab MCP Addon (zhb mod)

A fork of the [official Blender Lab MCP](https://projects.blender.org/lab/blender_mcp),
based on commit `dbbf836ad4b1025f14a2b3b504c43903f39e0b04`.
This is an independent modification by JustBelieve9, not an official Blender Lab release.
In Blender, the extension is listed as **MCP Project Ports**.

## Project ports (add-on 1.1.0)

- Configure and start the bridge in **3D View > N > MCP**.
- Store one host/port configuration per `.blend`, shared by all its local scenes.
- Run separate Blender processes on separate ports, with one MCP connection per agent.
- Stop stale connections before loading a different file; cancel delayed startup on Stop.
- Report port conflicts instead of silently routing to another instance.
- Use exclusive TCP binding on Windows to prevent two projects sharing one listener endpoint.

Download the add-on ZIP from [Releases](https://github.com/JustBelieve9/blender-lab-mcp-addon-zhb-mod/releases).
Blender 5.1+ is required; tested on Windows with Blender 5.2.2 LTS.

**[Installation and two-agent setup (Russian)](docs/project-ports-ru.md)**
| [Agent A config](examples/agent-project-a.json)
| [Agent B config](examples/agent-project-b.json)

The extension ID remains `mcp`: replace your existing Blender Lab MCP installation.
Do not enable both copies from different extension repositories; their operator IDs conflict.
An automatic upstream update can overwrite this fork. Keep a copy of your original add-on for rollback.

Project autostart is opt-in and requires both the project flag and the global Auto Start setting.
New/unsaved files never autostart. Change a running project's port with Stop > edit > Start.
MCP clients select the bridge via `BLENDER_MCP_HOST` and `BLENDER_MCP_PORT`.
The server's `--port` option controls its HTTP transport, not the Blender bridge.

## Building and testing

```text
blender --command extension build --source-dir addon/blender_mcp_addon --output-dir dist
blender --command extension validate dist/mcp-1.1.0.zip
blender -b --factory-startup --online-mode --python-exit-code 1 -P tests/test_project_settings_blender.py -- SCRATCH_DIR
python tests/test_project_ports_e2e.py --blender BLENDER_EXE --zip dist/mcp-1.1.0.zip --output EMPTY_TEST_DIR
```

Use an isolated `BLENDER_USER_*` profile for Blender tests. The E2E test creates its own profiles,
installs the built ZIP and checks two GUI instances through the official MCP SDK/stdio.
Install the local MCP server and SDK into a separate environment with `pip install -e ./mcp`.
The MCP server itself and its tool schemas remain upstream-compatible.

The original overview follows.

## Overview

A lightweight MCP (Model Context Protocol) server for Blender.
It offers a natural language interface with Blender's Python API,
improving access to documentation, and allowing users to explore
and understand complex setups.

Read the documentation at [blender.org/lab/mcp-server](https://www.blender.org/lab/mcp-server/)

----

The project is deliberately small, maintainable, and does no more than
necessary. It has two components that communicate over a TCP socket:

- A **Blender add-on** that runs inside Blender and executes requests.
- An **MCP server** that runs as a separate process, launched by the
  MCP client (e.g. [Llama.cpp](https://projects.blender.org/lab/blender_mcp/wiki/Llama.cpp)).

The data flow is:
```
MCP Client  ⇐ MCP/stdio ⇒  blender-mcp  ⇐ TCP socket ⇒  Blender Add-on
```


## Blender Add-on

Located in ``addon/blender_mcp_addon/``.

A Blender extension that allows the MCP server to communicate with a
running Blender instance. It must be installed and enabled for any of
the MCP tools to work.

The add-on provides a sidebar panel for the project's host, port and startup controls.
Preferences supply defaults for unconfigured files and polling/logging settings.

### Functionality Overview

Note that this is intended to be a fairly minimal add-on.

Connectivity
   - Auto-start (optional), is non-blocking any issues can be viewed from the preferences.
   - Configurable polling intervals (active and idle rates) from preferences to avoid excessive overhead.
   - Client timeout protection - stalled connections are evicted.
   - Start/stop operators accessible from the preferences panel.
   - Deferred responses are supported only by the interactive add-on server;
     background mode requires requests to complete synchronously and rejects deferred results.




## MCP Server

Located in ``mcp/blmcp/``, installed as a Python package with the
entry point ``blender-mcp``.

An MCP client launches this process and communicates with it over
stdio. The server connects to the add-on's TCP socket to relay
requests to Blender.

``mcp/blmcp/data/``
   Data files bundled with the package.

   - ``prompts.yml`` provides instructions sent to the LLM at
     connection time.
   - ``api/`` contains Blender Python API reference in RST format.
   - ``manual/`` contains Blender user manual excerpts in RST format.

``mcp/blmcp/tools/``
   Each tool is a single module, auto-discovered at startup.
   Modules ending in ``_toolcode`` contain code that runs inside
   Blender (sent to the add-on for execution) and are skipped during
   discovery.

``mcp/blmcp/tools_helpers/``
   Shared utilities used by tools. Tools should not import from each
   other; shared logic lives here instead.


### Tools

See [readme_tools.rst](readme_tools.rst) for the tools the MCP server exposes.
