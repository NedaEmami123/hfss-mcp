# ANSYS HFSS MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that enables AI assistants to interact with **ANSYS HFSS** through natural language commands, powered by [PyAEDT](https://aedt.docs.pyansys.com/) over gRPC.

Design antennas, assign boundaries, run simulations, and retrieve results — all by chatting with Claude.

---

## Compatible Clients

| Client | Supported |
|---|---|
| Claude Desktop | ✅ |
| Cursor | ✅ |
| Windsurf | ✅ |
| VS Code (Copilot) | ✅ |
| Any MCP-compatible client | ✅ |

---

## Capabilities

### Connection Management
| Tool | Description |
|---|---|
| `connect_to_hfss` | Connect to a running AEDT/HFSS session via gRPC port |
| `check_connection` | Check current connection status |
| `disconnect` | Release the HFSS connection without closing the project |

### Geometry
| Tool | Description |
|---|---|
| `create_box` | Create a rectangular box primitive |
| `create_cylinder` | Create a cylinder primitive |
| `create_sphere` | Create a sphere primitive |

### Materials
| Tool | Description |
|---|---|
| `list_materials` | List all materials in the active project |
| `assign_material` | Assign a material to an existing geometry object |

### Excitations
| Tool | Description |
|---|---|
| `add_wave_port` | Add a wave port excitation on a face |
| `add_lumped_port` | Add a lumped port excitation on a face |

### Solution Setup
| Tool | Description |
|---|---|
| `create_solution_setup` | Create an HFSS adaptive solution setup |
| `add_frequency_sweep` | Add a frequency sweep to a solution setup |

### Solve & Results
| Tool | Description |
|---|---|
| `solve` | Launch the HFSS solver |
| `get_solve_status` | Check solved/unsolved status of all setups |
| `get_s_parameters` | Retrieve S-parameter data for a solved sweep |
| `get_far_field` | Retrieve far-field radiation pattern data |

### Antenna Design Workflows
| Tool | Description |
|---|---|
| `design_patch_antenna` | Full coax-fed microstrip patch antenna at any frequency (Modal Network) |
| `design_dipole_antenna` | Full center-fed half-wave dipole at any frequency (Terminal Network) |
| `create_438mhz_dipole` | One-click 438 MHz half-wave dipole recreation |

### Scripting
| Tool | Description |
|---|---|
| `run_hfss_script` | Execute arbitrary PyAEDT/Python code against the live HFSS session |
| `get_script_result` | Poll the output of a background script job |

---

## Requirements

- ANSYS Electronics Desktop (AEDT) 2023 R1 or later with HFSS
- Python 3.10+
- PyAEDT (`ansys-aedt-core` or `pyaedt`)

---

## Installation

```bash
git clone https://github.com/NedaEmami123/hfss-mcp.git
cd hfss-mcp
python -m venv venv
venv\Scripts\activate       # Windows
pip install ansys-aedt-core mcp
```

---

## Configuration

Find your client's config file and add the `hfss-mcp` server entry:

| Client | Config file location |
|---|---|
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |
| Cursor | `%APPDATA%\Roaming\Cursor\User\globalStorage\cursor.mcp\mcp.json` |
| Windsurf | `%APPDATA%\Windsurf\User\globalStorage\codeium.windsurf\mcp_config.json` |
| VS Code | `.vscode/mcp.json` in your workspace |

Add this block:

```json
{
  "mcpServers": {
    "hfss-mcp": {
      "command": "C:/path/to/hfss-mcp/venv/Scripts/python.exe",
      "args": ["C:/path/to/hfss-mcp/hfss_mcp_server.py"],
      "env": {
        "ANSYSEM_ROOT261": "C:/Program Files/ANSYS Inc/v261/AnsysEM"
      }
    }
  }
}
```

> Replace `v261` with your installed AEDT version (e.g. `v232`, `v241`, `v251`).

---

## Usage

### 1. Start HFSS and get the gRPC port

Open ANSYS Electronics Desktop. In the **Message Manager**, look for:
```
gRPC server started on port 50051
```

### 2. Connect Claude to HFSS

In your chat with Claude:
> *"Connect to HFSS on port 50051"*

### 3. Design an antenna

> *"Design a 2.4 GHz patch antenna"*

Claude will:
- Create a new Modal Network design with all parameters as HFSS variables
- Build geometry: Substrate, Airbox, Ground plane, Patch, Coax probe, Teflon dielectric
- Assign boundaries: PerfE (ground & patch), coax shield, radiation boundary
- Add a wave port with integration line
- Create Setup1 (adaptive mesh) + frequency sweeps
- Insert a 3D far-field sphere
- Solve and generate S11, E-plane, H-plane, and 3D gain reports

> *"Design a 438 MHz dipole antenna"*

Claude will build a center-fed half-wave dipole in free space with a Terminal Network solution, lumped port, and full radiation analysis.

### 4. Poll for results (long jobs)

Long workflows run in the background. Claude will give you a `job_id` and poll automatically:
> *"Get script result for job_1"*

---

## Antenna Design Parameters (Patch — 2.4 GHz example)

All parameters are created as **HFSS design variables** so you can parametrically sweep them:

| Variable | Value | Description |
|---|---|---|
| `W` | 38.04 mm | Patch width (Pozar formula) |
| `L` | 29.44 mm | Patch length (Pozar formula) |
| `y0` | 5.59 mm | Coax feed offset |
| `h_s` | 1.6 mm | FR4 substrate thickness |
| `Ws` / `Ls` | 70 mm | Substrate & ground footprint |
| `h_stub` | 3 mm | Coax stub depth below ground |
| `r_probe` | 0.65 mm | Coax inner conductor radius |
| `r_diel` | 2 mm | Coax dielectric outer radius |
| `ab_lat` | 66 mm | Airbox half-width |

---

## License

Apache 2.0 — see [LICENSE](LICENSE)
