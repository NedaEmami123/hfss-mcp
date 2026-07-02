#!/usr/bin/env python3
"""ANSYS HFSS MCP Server - Connects AI clients to ANSYS HFSS via PyAEDT."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# PyAEDT prints INFO/ERROR lines directly to stdout which corrupts the MCP
# stdio JSON protocol. Patch stdout so only FastMCP's own writes go through;
# everything else is redirected to stderr.
import sys as _sys
import logging as _logging


class _StderrForwarder:
    """Forward all non-MCP writes (PyAEDT log lines) to stderr."""
    def __init__(self, real_stdout):
        self._out = real_stdout
        self._err = _sys.stderr
        # Expose .buffer so MCP's stdio transport can wrap us correctly
        self.buffer = real_stdout.buffer

    def write(self, data):
        stripped = data.lstrip()
        if stripped.startswith(("{", "[")):
            self._out.write(data)
        else:
            self._err.write(data)

    def flush(self):
        self._out.flush()
        self._err.flush()

    def fileno(self):
        return self._out.fileno()


_sys.stdout = _StderrForwarder(_sys.stdout)

# Also silence Python logging to stdout
for _name in ("pyaedt", "ansys", "root"):
    _log = _logging.getLogger(_name)
    _log.handlers = [_logging.StreamHandler(_sys.stderr)]
    _log.propagate = False

# ---------------------------------------------------------------------------
# Global state  (lives for the lifetime of this process)
# ---------------------------------------------------------------------------
_hfss = None
_status = "idle"   # "idle" | "connecting" | "connected" | "error"
_status_msg = ""
__version__ = "1.0.0"

mcp = FastMCP("ansys-hfss-mcp")

_DIR = Path(__file__).parent
_CONNFILE = _DIR / "hfss_conn.json"   # written after a successful connect


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _esc(s: str) -> str:
    """Escape a user string for safe embedding in a Python double-quoted string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _check_connection() -> str | None:
    if _hfss is None:
        if _status == "connecting":
            return _json({"ok": False, "error": "Still connecting — call check_connection in a few seconds."})
        return _json({"ok": False, "error": "Not connected to HFSS. Call connect_to_hfss first."})
    return None


def _do_connect(port: int, project: str, design: str) -> None:
    """Background thread: connect PyAEDT and update globals."""
    global _hfss, _status, _status_msg
    try:
        from ansys.aedt.core import Hfss
    except ImportError:
        try:
            from pyaedt import Hfss  # type: ignore[no-redef]
        except ImportError:
            _status = "error"
            _status_msg = "PyAEDT not installed."
            return
    try:
        if _hfss is not None:
            try:
                _hfss.release_desktop(close_projects=False, close_desktop=False)
            except Exception:
                pass
            _hfss = None

        h = Hfss(
            project=project or None,
            design=design or None,
            machine="localhost",
            port=port,
            close_on_exit=False,
        )
        _hfss = h
        _status = "connected"
        _status_msg = f"{h.project_name} / {h.design_name}"

        # Persist so the user knows it worked across restarts
        _CONNFILE.write_text(json.dumps({
            "port": port,
            "project": h.project_name,
            "design": h.design_name,
            "aedt_version": h.aedt_version_id,
            "solution_type": _safe_sol_type(h),
        }))
    except Exception as e:
        _status = "error"
        _status_msg = str(e)
        _hfss = None


def _safe_sol_type(h) -> str:
    try:
        return h.solution_type
    except Exception:
        try:
            return h.odesign.GetSolutionType()
        except Exception:
            return "unknown"


# ---------------------------------------------------------------------------
# Connection tools
# ---------------------------------------------------------------------------


@mcp.tool()
def connect_to_hfss(port: int = 50051, project: str = "", design: str = "") -> str:
    """Connect to a running AEDT/HFSS session via gRPC.

    AEDT starts a gRPC server automatically — check the HFSS Message Manager
    for "gRPC server started on port XXXXX" and pass that port.

    Connection runs in the background (PyAEDT takes ~15 s to initialise).
    Call check_connection after ~20 seconds to confirm success.

    Args:
        port: gRPC port shown in HFSS Message Manager (default 50051).
        project: HFSS project name (empty = active project).
        design: HFSS design name (empty = active/first design).
    """
    global _status, _status_msg
    if _status == "connecting":
        return _json({"ok": True, "status": "connecting", "message": "Already connecting — call check_connection."})

    _status = "connecting"
    _status_msg = ""
    t = threading.Thread(target=_do_connect, args=(port, project, design), daemon=True)
    t.start()
    return _json({
        "ok": True,
        "status": "connecting",
        "message": f"Connecting to HFSS on port {port} in the background. Call check_connection in ~20 seconds.",
    })


@mcp.tool()
def check_connection() -> str:
    """Return current connection status. Call ~20 s after connect_to_hfss to confirm."""
    if _status == "connecting":
        return _json({"ok": True, "connected": False, "status": "connecting", "message": "Still initialising — try again in a few seconds."})
    if _status == "error":
        return _json({"ok": False, "connected": False, "status": "error", "error": _status_msg})
    if _hfss is None:
        # Check if a previous session file exists
        if _CONNFILE.exists():
            saved = json.loads(_CONNFILE.read_text())
            return _json({"ok": True, "connected": False, "status": "idle",
                          "message": "Process restarted. Call connect_to_hfss to reconnect.",
                          "last_session": saved})
        return _json({"ok": True, "connected": False, "status": "idle"})
    try:
        return _json({
            "ok": True,
            "connected": True,
            "status": "connected",
            "aedt_version": _hfss.aedt_version_id,
            "project": _hfss.project_name,
            "design": _hfss.design_name,
            "solution_type": _safe_sol_type(_hfss),
        })
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@mcp.tool()
def disconnect() -> str:
    """Release the HFSS/AEDT connection without closing the project."""
    global _hfss, _status, _status_msg
    if _hfss is None:
        return _json({"ok": False, "error": "Not connected."})
    try:
        _hfss.release_desktop(close_projects=False, close_desktop=False)
    except Exception:
        pass
    _hfss = None
    _status = "idle"
    _status_msg = ""
    try:
        _CONNFILE.unlink()
    except Exception:
        pass
    return _json({"ok": True, "message": "Disconnected from HFSS."})


# ---------------------------------------------------------------------------
# Geometry tools
# ---------------------------------------------------------------------------


@mcp.tool()
def create_box(name: str, origin: list[float], dimensions: list[float], material: str = "vacuum") -> str:
    """Create a rectangular box primitive.

    Args:
        name: Object name.
        origin: [x, y, z] corner in mm.
        dimensions: [dx, dy, dz] extents in mm.
        material: Material name (default "vacuum").
    """
    err = _check_connection()
    if err:
        return err
    try:
        if len(origin) != 3 or len(dimensions) != 3:
            return _json({"ok": False, "error": "origin and dimensions must each have 3 elements."})
        obj = _hfss.modeler.create_box(origin, dimensions, name=_esc(name), material=_esc(material))
        return _json({"ok": True, "name": obj.name, "id": obj.id})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@mcp.tool()
def create_cylinder(name: str, center: list[float], radius: float, height: float, axis: str = "Z", material: str = "vacuum") -> str:
    """Create a cylinder primitive.

    Args:
        name: Object name.
        center: [x, y, z] base center in mm.
        radius: Radius in mm.
        height: Height in mm.
        axis: Extrusion axis — "X", "Y", or "Z".
        material: Material name.
    """
    err = _check_connection()
    if err:
        return err
    try:
        axis_map = {"X": 0, "Y": 1, "Z": 2}
        ax = axis_map.get(axis.upper())
        if ax is None:
            return _json({"ok": False, "error": "axis must be X, Y, or Z."})
        obj = _hfss.modeler.create_cylinder(ax, center, radius, height, name=_esc(name), material=_esc(material))
        return _json({"ok": True, "name": obj.name, "id": obj.id})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@mcp.tool()
def create_sphere(name: str, center: list[float], radius: float, material: str = "vacuum") -> str:
    """Create a sphere primitive.

    Args:
        name: Object name.
        center: [x, y, z] center in mm.
        radius: Radius in mm.
        material: Material name.
    """
    err = _check_connection()
    if err:
        return err
    try:
        obj = _hfss.modeler.create_sphere(center, radius, name=_esc(name), material=_esc(material))
        return _json({"ok": True, "name": obj.name, "id": obj.id})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Material tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_materials() -> str:
    """List all materials available in the active project."""
    err = _check_connection()
    if err:
        return err
    try:
        materials = list(_hfss.materials.material_keys)
        return _json({"ok": True, "count": len(materials), "materials": sorted(materials)})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@mcp.tool()
def assign_material(object_name: str, material: str) -> str:
    """Assign a material to an existing geometry object.

    Args:
        object_name: Name of the 3-D object in the modeler.
        material: Material name (must exist in the project material library).
    """
    err = _check_connection()
    if err:
        return err
    try:
        obj = _hfss.modeler[_esc(object_name)]
        obj.material_name = _esc(material)
        return _json({"ok": True, "object": object_name, "material": material})
    except KeyError:
        return _json({"ok": False, "error": f"Object '{object_name}' not found."})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Excitation tools
# ---------------------------------------------------------------------------


@mcp.tool()
def add_wave_port(name: str, face_id: int, modes: int = 1, deembed: float = 0.0) -> str:
    """Add a wave port excitation on a face.

    Args:
        name: Port name.
        face_id: Integer face ID from the modeler.
        modes: Number of modes.
        deembed: De-embed distance in mm (0 = no de-embed).
    """
    err = _check_connection()
    if err:
        return err
    try:
        port = _hfss.wave_port(face_id, name=_esc(name), modes=modes, deembed=deembed if deembed else None)
        return _json({"ok": True, "port": port.name})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@mcp.tool()
def add_lumped_port(name: str, face_id: int, impedance: float = 50.0) -> str:
    """Add a lumped port excitation on a face.

    Args:
        name: Port name.
        face_id: Integer face ID from the modeler.
        impedance: Reference impedance in Ohms (default 50).
    """
    err = _check_connection()
    if err:
        return err
    try:
        port = _hfss.lumped_port(face_id, name=_esc(name), impedance=impedance)
        return _json({"ok": True, "port": port.name})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Solution setup tools
# ---------------------------------------------------------------------------


@mcp.tool()
def create_solution_setup(name: str = "Setup1", frequency_ghz: float = 1.0, max_passes: int = 6, max_delta_s: float = 0.02) -> str:
    """Create an HFSS solution setup (adaptive mesh pass).

    Args:
        name: Setup name.
        frequency_ghz: Adaptive mesh frequency in GHz.
        max_passes: Maximum number of adaptive passes.
        max_delta_s: Convergence criterion (max delta S).
    """
    err = _check_connection()
    if err:
        return err
    try:
        setup = _hfss.create_setup(name=_esc(name))
        setup.props["Frequency"] = f"{frequency_ghz}GHz"
        setup.props["MaximumPasses"] = max_passes
        setup.props["MaxDeltaS"] = max_delta_s
        setup.update()
        return _json({"ok": True, "setup": name, "frequency_ghz": frequency_ghz})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@mcp.tool()
def add_frequency_sweep(setup_name: str, sweep_name: str = "Sweep1", start_ghz: float = 0.5, stop_ghz: float = 2.0, step_ghz: float = 0.05, sweep_type: str = "Interpolating") -> str:
    """Add a frequency sweep to an existing solution setup.

    Args:
        setup_name: Name of the parent solution setup.
        sweep_name: Name for this sweep.
        start_ghz: Start frequency in GHz.
        stop_ghz: Stop frequency in GHz.
        step_ghz: Step size in GHz.
        sweep_type: "Fast", "Interpolating", or "Discrete".
    """
    err = _check_connection()
    if err:
        return err
    try:
        _hfss.create_linear_step_sweep(
            _esc(setup_name), "GHz", start_ghz, stop_ghz, step_ghz,
            name=_esc(sweep_name), sweep_type=_esc(sweep_type),
        )
        return _json({"ok": True, "setup": setup_name, "sweep": sweep_name, "start_ghz": start_ghz, "stop_ghz": stop_ghz})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Solve tools
# ---------------------------------------------------------------------------


@mcp.tool()
def solve(setup_name: str = "") -> str:
    """Launch the HFSS solver for one or all setups.

    Args:
        setup_name: Name of the setup to solve, or empty to solve all setups.
    """
    err = _check_connection()
    if err:
        return err
    try:
        if setup_name:
            _hfss.analyze_setup(_esc(setup_name))
        else:
            _hfss.analyze_all()
        return _json({"ok": True, "message": "Solve completed."})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@mcp.tool()
def get_solve_status() -> str:
    """Return the solved/unsolved status of all solution setups."""
    err = _check_connection()
    if err:
        return err
    try:
        statuses = {}
        for setup in _hfss.setups:
            statuses[setup.name] = {
                "solved": setup.is_solved,
                "sweeps": [sw.name for sw in getattr(setup, "sweeps", [])],
            }
        return _json({"ok": True, "setups": statuses})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Results tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_s_parameters(setup_name: str, sweep_name: str, expressions: list[str] | None = None) -> str:
    """Retrieve S-parameter data for a solved sweep.

    Args:
        setup_name: Solution setup name.
        sweep_name: Frequency sweep name.
        expressions: S-param expressions e.g. ["S(1,1)", "S(2,1)"] — empty = all.
    """
    err = _check_connection()
    if err:
        return err
    try:
        solution = f"{_esc(setup_name)} : {_esc(sweep_name)}"
        if not expressions:
            expressions = _hfss.get_traces_for_plot(get_self_terms=True, get_mutual_terms=True, solution=solution)
        data = {}
        for expr in expressions:
            try:
                sol_data = _hfss.post.get_solution_data(expressions=expr, setup_sweep_name=solution)
                data[expr] = {"frequencies": sol_data.primary_sweep_values, "values": sol_data.data_magnitude()}
            except Exception as ex:
                data[expr] = {"error": str(ex)}
        return _json({"ok": True, "solution": solution, "s_parameters": data})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@mcp.tool()
def get_far_field(setup_name: str, sweep_name: str, sphere_name: str = "3D", freq_ghz: float | None = None) -> str:
    """Retrieve far-field radiation pattern data.

    Args:
        setup_name: Solution setup name.
        sweep_name: Frequency sweep name.
        sphere_name: Far-field sphere name defined in the design (default "3D").
        freq_ghz: Frequency in GHz to evaluate (None = adaptive frequency).
    """
    err = _check_connection()
    if err:
        return err
    try:
        solution = f"{_esc(setup_name)} : {_esc(sweep_name)}"
        ff_data = _hfss.post.get_far_field_data(
            setup_sweep_name=solution,
            sphere_name=_esc(sphere_name),
            freq=f"{freq_ghz}GHz" if freq_ghz else None,
        )
        return _json({"ok": True, "solution": solution, "sphere": sphere_name, "far_field": str(ff_data)})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# High-level patch-antenna workflow
# ---------------------------------------------------------------------------

def _build_patch_antenna(job_id: str, freq_ghz: float, design_name: str, project: str, port: int) -> None:
    """Coax-fed patch antenna — Modal Network, parametric variables, mirrors HFSSDesign2."""
    import math, traceback as _tb
    output_lines: list[str] = []

    def log(msg: str) -> None:
        output_lines.append(msg)
        _script_results[job_id] = {"status": "running", "output": "\n".join(output_lines)}

    try:
        try:
            from ansys.aedt.core import Hfss
        except ImportError:
            from pyaedt import Hfss  # type: ignore[no-redef]

        import time as _time

        # ── 1. Create Modal design using the existing _hfss connection ────────
        log(f"Phase 1 — creating design '{design_name}' (Modal Network) …")
        oproject = _hfss._oproject
        proj_name = _hfss.project_name

        # ── 1b. Create design via subprocess so gRPC SetActiveDesign works ──────
        # When a *different* process creates the design, HFSS GUI activates it and
        # our main process can then SetActiveDesign successfully. Same-process
        # InsertDesign does not update the gRPC client's active-design context.
        import subprocess, sys

        log(f"Phase 1 — creating design '{design_name}' via subprocess …")
        helper = (
            "from ansys.aedt.core import Hfss\n"
            f"h = Hfss(project={repr(proj_name)}, machine='localhost', port={port}, close_on_exit=False)\n"
            "try:\n"
            f"    h._oproject.DeleteDesign({repr(design_name)})\n"
            "    print('deleted')\n"
            "except Exception as e:\n"
            "    print('no delete:', e)\n"
            f"h._oproject.InsertDesign('HFSS', {repr(design_name)}, 'HFSS Modal Network', '')\n"
            f"print('done', h.project_name)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", helper],
            capture_output=True, text=True, timeout=120,
        )
        log(f"Subprocess stdout: {result.stdout.strip()}")
        if result.stderr:
            log(f"Subprocess stderr: {result.stderr.strip()[-300:]}")
        if result.returncode != 0:
            raise RuntimeError(f"Subprocess failed (rc={result.returncode})")
        _time.sleep(3)

        # ── 2. Connect PyAEDT to the newly created design ────────────────────
        log("Phase 2 — connecting PyAEDT to the new design …")
        h = Hfss(project=proj_name, design=design_name,
                 machine="localhost", port=port, close_on_exit=False)
        if h._odesign is None:
            raise RuntimeError("PyAEDT _odesign is None after subprocess design creation.")
        sol = h._odesign.GetSolutionType()
        log(f"Connected: design='{h.design_name}'  solution type='{sol}'")
        if "Modal" not in sol:
            raise RuntimeError(f"Wrong solution type '{sol}' — expected HFSS Modal Network.")

        mod = h.modeler
        existing = mod.object_names
        if existing:
            log(f"Clearing existing objects: {existing}")
            mod.delete(existing)

        # ── 3. Compute antenna parameters (Pozar formulas, FR4 @ freq_ghz) ───
        f_hz = freq_ghz * 1e9
        c, er, h_s = 3e8, 4.4, 1.6

        W   = (c / (2*f_hz)) * math.sqrt(2/(er+1)) * 1e3
        erc = (er+1)/2 + (er-1)/2 * (1+12*h_s/W)**-0.5
        dL  = 0.412*h_s*(erc+0.3)*(W/h_s+0.264) / ((erc-0.258)*(W/h_s+0.8))
        L   = (c / (2*f_hz*math.sqrt(erc))) * 1e3 - 2*dL
        W, L    = round(W, 3), round(L, 3)
        y0      = round(L / 5.27, 3)
        Ws = Ls = 70.0
        h_stub  = 3.0
        ab_lat  = 66.0
        air_above = 31.0
        r_probe = 0.65
        r_diel  = 2.0
        airbox_h = round(h_stub + h_s + air_above, 3)

        log(f"Parameters: W={W}mm  L={L}mm  y0={y0}mm  h_s={h_s}mm  airbox_h={airbox_h}mm")

        # ── 4. Create HFSS design variables (same as HFSSDesign2) ────────────
        log("Creating design variables …")
        h["W"]         = f"{W}mm"
        h["L"]         = f"{L}mm"
        h["y0"]        = f"{y0}mm"
        h["h_s"]       = f"{h_s}mm"
        h["Ws"]        = f"{Ws}mm"
        h["Ls"]        = f"{Ls}mm"
        h["h_stub"]    = f"{h_stub}mm"
        h["ab_lat"]    = f"{ab_lat}mm"
        h["air_above"] = f"{air_above}mm"
        h["airbox_h"]  = f"{airbox_h}mm"
        h["r_probe"]   = f"{r_probe}mm"
        h["r_diel"]    = f"{r_diel}mm"
        log("Variables: W, L, y0, h_s, Ws, Ls, h_stub, ab_lat, air_above, airbox_h, r_probe, r_diel")

        # ── 5. Geometry — float values used directly for reliability ──────────
        # FR4 substrate slab: z = 0 to h_s
        mod.create_box(
            [-Ws/2, -Ls/2, 0],
            [Ws, Ls, h_s],
            name="Substrate", material="FR4_epoxy",
        )
        log("Substrate OK")

        # Vacuum airbox: z = -h_stub to h_s+air_above
        mod.create_box(
            [-ab_lat, -ab_lat, -h_stub],
            [2*ab_lat, 2*ab_lat, airbox_h],
            name="Airbox", material="vacuum",
        )
        log("Airbox OK")

        # Ground plane — horizontal sheet at z=0 in XY plane (WhichAxis=Z via COM)
        oeditor = mod.oeditor
        oeditor.CreateRectangle([
            "NAME:RectangleParameters",
            "IsCovered:=", True,
            "XStart:=", f"{-Ws/2}mm",
            "YStart:=", f"{-Ls/2}mm",
            "ZStart:=", "0mm",
            "Width:=", f"{Ws}mm",
            "Height:=", f"{Ls}mm",
            "WhichAxis:=", "Z",
        ], [
            "NAME:Attributes",
            "Name:=", "Ground",
            "Flags:=", "",
            "Color:=", "(143 175 143)",
            "Transparency:=", 0,
            "PartCoordinateSystem:=", "Global",
            "UDMId:=", "",
            "MaterialValue:=", '"vacuum"',
            "SolveInside:=", False,
        ])
        log("Ground OK")

        # Radiating patch — horizontal sheet at z=h_s in XY plane (WhichAxis=Z via COM)
        oeditor.CreateRectangle([
            "NAME:RectangleParameters",
            "IsCovered:=", True,
            "XStart:=", f"{-W/2}mm",
            "YStart:=", f"{-L/2}mm",
            "ZStart:=", f"{h_s}mm",
            "Width:=", f"{W}mm",
            "Height:=", f"{L}mm",
            "WhichAxis:=", "Z",
        ], [
            "NAME:Attributes",
            "Name:=", "Patch",
            "Flags:=", "",
            "Color:=", "(255 128 65)",
            "Transparency:=", 0,
            "PartCoordinateSystem:=", "Global",
            "UDMId:=", "",
            "MaterialValue:=", '"vacuum"',
            "SolveInside:=", False,
        ])
        log("Patch OK")

        # Coax probe — copper cylinder from z=-h_stub through substrate to z=h_s
        mod.create_cylinder(2, [0, -y0, -h_stub], r_probe, h_stub + h_s,
                            name="Probe", material="copper")
        log("Probe OK")

        # Coax dielectric — Teflon cylinder below ground: z=-h_stub to z=0
        mod.create_cylinder(2, [0, -y0, -h_stub], r_diel, h_stub,
                            name="CoaxDielectric", material="Teflon (tm)")
        log("CoaxDielectric OK")

        # Hollow out CoaxDielectric around probe (keep probe)
        mod.subtract("CoaxDielectric", ["Probe"], keep_originals=True)
        log("CoaxDielectric annularised OK")

        # Clearance hole through substrate for probe
        _sh = mod.create_cylinder(2, [0, -y0, 0], r_probe, h_s, name="_sub_hole")
        mod.subtract("Substrate", ["_sub_hole"], keep_originals=False)
        log("Substrate clearance hole OK")

        # Clearance hole in ground sheet for probe — horizontal circle via COM
        oeditor.CreateCircle([
            "NAME:CircleParameters",
            "IsCovered:=", True,
            "XCenter:=", "0mm",
            "YCenter:=", f"{-y0}mm",
            "ZCenter:=", "0mm",
            "Radius:=", f"{r_probe}mm",
            "WhichAxis:=", "Z",
            "NumSegments:=", "0",
        ], [
            "NAME:Attributes",
            "Name:=", "_gnd_hole",
            "Flags:=", "",
            "Color:=", "(143 175 143)",
            "Transparency:=", 0,
            "PartCoordinateSystem:=", "Global",
            "UDMId:=", "",
            "MaterialValue:=", '"vacuum"',
            "SolveInside:=", False,
        ])
        mod.subtract("Ground", ["_gnd_hole"], keep_originals=False)
        log("Ground clearance hole OK")

        log(f"All objects: {mod.object_names}")

        # ── 4. Boundaries ─────────────────────────────────────────────────────
        h.assign_perfecte_to_sheets("Ground", name="PerfE_Ground")
        log("PerfE_Ground OK")

        h.assign_perfecte_to_sheets("Patch", name="PerfE_Patch")
        log("PerfE_Patch OK")

        # Shield = largest-area face of CoaxDielectric (outer cylindrical surface)
        cdiel_obj = mod["CoaxDielectric"]
        shield_face = max(cdiel_obj.faces, key=lambda f: f.area)
        h.assign_perfecte_to_sheets([shield_face.id], name="PerfE_CoaxShield")
        log(f"PerfE_CoaxShield OK (face {shield_face.id}, area={shield_face.area:.1f})")

        h.assign_radiation_boundary_to_objects("Airbox", name="Rad_Airbox")
        log("Rad_Airbox OK")

        # ── 5. Wave port WITH integration line via direct COM call ────────────
        # Integration line: from probe edge (r_probe) to outer conductor edge (r_diel)
        # Both points at z=-h_stub (port face), y=-y0 (feed axis)
        # Using "Start/End" mm-string format — HFSS scripting recorder format
        bot_face = min(cdiel_obj.faces, key=lambda f: f.center[2])
        log(f"Wave port face: id={bot_face.id}  z={bot_face.center[2]:.2f}  area={bot_face.area:.2f}")

        obound = h._odesign.GetModule("BoundarySetup")
        obound.AssignWavePort([
            "NAME:WavePort_Coax",
            "Faces:=", [bot_face.id],
            "NumModes:=", 1,
            "RenormalizeAllTerminals:=", True,
            "UseLineModeAlignment:=", False,
            "DoDeembed:=", False,
            "ShowReporterFilter:=", False,
            "ReporterFilter:=", [True],
            "UseAnalyticAlignment:=", False,
            "Modes:=", [
                "NAME:Mode1",
                "ModeNum:=", 1,
                "UseIntLine:=", True,
                "IntLine:=", [
                    "NAME:IntLine",
                    "Start:=", [f"{r_probe}mm", f"{-y0}mm", f"{-h_stub}mm"],
                    "End:=",   [f"{r_diel}mm",  f"{-y0}mm", f"{-h_stub}mm"],
                ],
                "AlignmentGroup:=", 0,
                "CharImp:=", "Zpi",
                "RenormImp:=", "50ohm",
            ],
            "SpecifyWaveDirection:=", False,
            "WaveDirectionComputed:=", False,
            "SpecifiedWaveDirectionFlip:=", False,
        ])

        # Wave ports are excitations, not boundaries — verify via GetExcitations
        all_excitations = list(obound.GetExcitations())
        log(f"Excitations after AssignWavePort: {all_excitations}")
        if not any("WavePort_Coax" in e for e in all_excitations):
            raise RuntimeError(f"WavePort_Coax not found in excitations: {all_excitations}")

        # ── 6. Solution setup ─────────────────────────────────────────────────
        setup = h.create_setup(name="Setup1")
        setup.props["Frequency"] = f"{freq_ghz}GHz"
        setup.props["MaximumPasses"] = 20
        setup.props["MaxDeltaS"] = 0.02
        setup.update()
        log("Setup1 OK")

        # ── 7. Sweeps — matches HFSSDesign2 exactly ──────────────────────────
        # Sweep1: interpolating, 1.5-3.5 GHz, 401 points, SaveFields=True (for S11)
        h.create_linear_count_sweep(
            "Setup1", "GHz", 1.5, 3.5, 401,
            name="Sweep1", sweep_type="Interpolating", save_fields=True,
        )
        log("Sweep1 (1.5–3.5 GHz, 401pts, SaveFields) OK")

        # Radiation sweep: 0.05 GHz step, SaveRadFields=True (for far-field plots)
        sweep2 = h.create_linear_step_sweep(
            "Setup1", "GHz", 1.5, 3.5, 0.05,
            name="Sweep_Rad", sweep_type="Interpolating",
        )
        if sweep2:
            sweep2.props["SaveRadFields"] = True
            sweep2.props["SaveFields"] = False
            sweep2.update()
        log("Sweep_Rad (0.05GHz step, SaveRadFields) OK")

        # ── 8. Far-field infinite sphere BEFORE solve ─────────────────────────
        h.insert_infinite_sphere(
            phi_start=0, phi_stop=360, phi_step=2,
            theta_start=0, theta_stop=180, theta_step=2,
            name="3D",
        )
        log("Infinite sphere '3D' inserted (Phi 0-360°, Theta 0-180°, 2° step)")

        # ── 9. Save ───────────────────────────────────────────────────────────
        h.save_project()
        log("Project saved.")

        # ── 10. Solve ─────────────────────────────────────────────────────────
        log("Solving … (several minutes)")
        h.analyze_setup("Setup1")
        log("Solve complete.")

        # ── 11. Reports ───────────────────────────────────────────────────────
        # For Modal solution type, S-param port name format is "WavePort_Coax:1"
        # Use GetExcitations; fall back to hardcoded name if empty (timing issue)
        excitations = list(obound.GetExcitations())
        log(f"Excitations after solve: {excitations}")
        port_mode = next((e for e in excitations if ":" in e), "WavePort_Coax:1")
        s11_expr  = f"dB(S({port_mode},{port_mode}))"
        log(f"S11 expression: {s11_expr}")

        # S11 return loss — rectangular plot vs Sweep1
        try:
            rep = h.post.reports_by_category.standard(
                expressions=s11_expr, setup="Setup1 : Sweep1")
            rep.create("S11_Report")
            log("S11_Report OK")
        except Exception as ex:
            log(f"  S11_Report: {ex}")

        # 2D E-plane (Phi=90°) polar plot
        try:
            rep_e = h.post.reports_by_category.far_field(
                expressions="dB(GainTotal)",
                setup="Setup1 : LastAdaptive",
                sphere_name="3D",
            )
            rep_e.primary_sweep = "Theta"
            rep_e.variations    = {"Phi": "90deg", "Freq": f"{freq_ghz}GHz"}
            rep_e.create("RadiationPattern_Phi90")
            log("E-plane (Phi=90°) OK")
        except Exception as ex:
            log(f"  E-plane: {ex}")

        # 2D H-plane (Phi=0°) polar plot
        try:
            rep_h = h.post.reports_by_category.far_field(
                expressions="dB(GainTotal)",
                setup="Setup1 : LastAdaptive",
                sphere_name="3D",
            )
            rep_h.primary_sweep = "Theta"
            rep_h.variations    = {"Phi": "0deg", "Freq": f"{freq_ghz}GHz"}
            rep_h.create("RadiationPattern_Phi0")
            log("H-plane (Phi=0°) OK")
        except Exception as ex:
            log(f"  H-plane: {ex}")

        # 3D polar gain pattern — use create_report with plot_type="3D Polar Plot"
        try:
            h.post.create_report(
                expressions=["dB(GainTotal)"],
                setup_sweep_name="Setup1 : LastAdaptive",
                report_category="Far Fields",
                plot_type="3D Polar Plot",
                primary_sweep_variable="Theta",
                secondary_sweep_variable="Phi",
                context="3D",
                plot_name="RadiationPattern_3D",
                show=False,
            )
            log("3D Polar radiation pattern OK")
        except Exception as ex:
            log(f"  3D polar: {ex}")

        h.save_project()
        log("=== Complete! ===")
        _script_results[job_id] = {"status": "done", "output": "\n".join(output_lines)}

    except Exception as e:
        output_lines.append(f"FATAL ERROR: {e}")
        output_lines.append(_tb.format_exc())
        _script_results[job_id] = {"status": "error", "output": "\n".join(output_lines)}


@mcp.tool()
def design_patch_antenna(
    freq_ghz: float = 2.4,
    design_name: str = "HFSSDesign2",
    project: str = "",
    port: int = 50051,
) -> str:
    """Design a complete coax-fed microstrip patch antenna from scratch.

    Triggered by requests like:
      "design a 2.4 GHz patch antenna — geometry, materials, solution, solve,
       return loss and radiation pattern in 2D and 3D"

    Workflow (runs in background — poll with get_script_result):
      1. Create a NEW HFSS Modal design named <design_name>
      2. Compute patch dimensions (Pozar formulas) for <freq_ghz> on FR4
      3. Build geometry: Substrate, Airbox, Ground, Patch, Probe, CoaxDielectric
      4. Assign materials: FR4_epoxy, copper, Teflon
      5. Assign boundaries: PerfE_Ground, PerfE_Patch, PerfE_CoaxShield, Rad_Airbox
      6. Add wave port (WavePort_Coax) at bottom of coax stub
      7. Create Setup1 (adaptive at freq_ghz, 20 passes, ΔS=0.02)
      8. Add Sweep1 (1.5–3.5 GHz interpolating)
      9. Solve
     10. Create reports: S11 return loss, E-plane, H-plane, 3D gain

    Args:
        freq_ghz: Design frequency in GHz (default 2.4).
        design_name: HFSS design name to create (default "PatchAntenna").
        project: Project name (empty = active project).
        port: gRPC port AEDT is listening on (default 50051).
    """
    global _script_counter
    err = _check_connection()
    if err:
        return err
    _script_counter += 1
    job_id = f"job_{_script_counter}"
    _script_results[job_id] = {"status": "running", "output": "Starting patch antenna workflow…"}
    t = threading.Thread(
        target=_build_patch_antenna,
        args=(job_id, freq_ghz, design_name, project, port),
        daemon=True,
    )
    t.start()
    return _json({
        "ok": True,
        "job_id": job_id,
        "message": (
            f"Patch antenna workflow started for {freq_ghz} GHz. "
            f"Poll with get_script_result('{job_id}') every 30 s — "
            "solve takes several minutes."
        ),
    })


# ---------------------------------------------------------------------------
# Half-wave dipole antenna workflow  (HFSSDesign1)
# ---------------------------------------------------------------------------

def _build_dipole_antenna(job_id: str, freq_ghz: float, design_name: str, project: str, port: int) -> None:
    """Center-fed half-wave dipole in free space — mirrors HFSSDesign1."""
    import math, traceback as _tb
    output_lines: list[str] = []

    def log(msg: str) -> None:
        output_lines.append(msg)
        _script_results[job_id] = {"status": "running", "output": "\n".join(output_lines)}

    try:
        try:
            from ansys.aedt.core import Hfss
        except ImportError:
            from pyaedt import Hfss  # type: ignore[no-redef]

        import time as _time

        # ── 1. Create Terminal design using the existing _hfss connection ─────
        # Avoids spawning a subprocess (which would block on the same gRPC port).
        log(f"Phase 1 — creating design '{design_name}' (Terminal Network) …")
        oproject = _hfss._oproject
        proj_name = _hfss.project_name

        import subprocess, sys

        log(f"Phase 1 — creating Terminal design '{design_name}' via subprocess …")
        helper = (
            "from ansys.aedt.core import Hfss\n"
            f"h = Hfss(project={repr(proj_name)}, machine='localhost', port={port}, close_on_exit=False)\n"
            "try:\n"
            f"    h._oproject.DeleteDesign({repr(design_name)})\n"
            "    print('deleted')\n"
            "except Exception as e:\n"
            "    print('no delete:', e)\n"
            f"h._oproject.InsertDesign('HFSS', {repr(design_name)}, 'HFSS Terminal Network', '')\n"
            f"print('done', h.project_name)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", helper],
            capture_output=True, text=True, timeout=120,
        )
        log(f"Subprocess stdout: {result.stdout.strip()}")
        if result.stderr:
            log(f"Subprocess stderr: {result.stderr.strip()[-300:]}")
        if result.returncode != 0:
            raise RuntimeError(f"Subprocess failed (rc={result.returncode})")
        _time.sleep(3)

        log("Phase 2 — connecting PyAEDT to the new design …")
        h = Hfss(project=proj_name, design=design_name,
                 machine="localhost", port=port, close_on_exit=False)
        if h._odesign is None:
            raise RuntimeError("PyAEDT _odesign is None after subprocess design creation.")
        sol = h._odesign.GetSolutionType()
        log(f"Connected: design='{h.design_name}'  solution type='{sol}'")
        if "Terminal" not in sol:
            raise RuntimeError(f"Wrong solution type '{sol}' — expected HFSS Terminal Network.")

        mod = h.modeler
        existing = mod.object_names
        if existing:
            log(f"Clearing existing objects: {existing}")
            mod.delete(existing)

        # ── 3. Dipole geometry parameters ─────────────────────────────────────
        c_light = 3e8
        f_hz = freq_ghz * 1e9
        lam_mm = c_light / f_hz * 1e3          # wavelength in mm
        arm_len = round(lam_mm / 4, 2)         # each arm ≈ λ/4
        r_wire  = 2.0                           # wire radius (mm)
        gap     = 2.0                           # feed gap (mm)
        # Airbox: λ/2 clearance on all sides from the dipole tip
        ab_half = round(lam_mm / 2 + arm_len + gap / 2, 1)

        log(f"λ={lam_mm:.1f} mm  arm_len={arm_len} mm  airbox_half={ab_half} mm  gap={gap} mm")

        # ── 4. Geometry ───────────────────────────────────────────────────────
        # Airbox (vacuum)
        mod.create_box(
            [-ab_half, -ab_half, -ab_half],
            [2*ab_half, 2*ab_half, 2*ab_half],
            name="Airbox", material="vacuum",
        )
        log("Airbox OK")

        # Upper arm: base at z=+gap/2, extends +arm_len along Z
        mod.create_cylinder(2, [0, 0, gap/2], r_wire, arm_len,
                            name="Arm_Upper", material="copper")
        log("Arm_Upper OK")

        # Lower arm: base at z=-(gap/2+arm_len), extends +arm_len along Z
        mod.create_cylinder(2, [0, 0, -(gap/2 + arm_len)], r_wire, arm_len,
                            name="Arm_Lower", material="copper")
        log("Arm_Lower OK")

        # Feed sheet in the ZX plane spanning the gap between the two arms.
        # integer 2 = ZX plane; origin, [z_size, x_size]
        # We want: x from -r_wire to +r_wire, z from -gap/2 to +gap/2
        mod.create_rectangle(
            2,
            [-r_wire, 0, -gap/2],
            [gap, 2*r_wire],
            name="FeedSheet",
        )
        log("FeedSheet OK")

        log(f"All objects: {mod.object_names}")

        # ── 5. Boundaries ─────────────────────────────────────────────────────
        # Perfect E on both arms (PEC wire)
        h.assign_perfecte_to_sheets("Arm_Upper", name="PerfE_ArmUpper")
        log("PerfE_ArmUpper OK")
        h.assign_perfecte_to_sheets("Arm_Lower", name="PerfE_ArmLower")
        log("PerfE_ArmLower OK")

        # Radiation boundary on all faces of Airbox
        h.assign_radiation_boundary_to_objects("Airbox", name="Rad_Airbox")
        log("Rad_Airbox OK")

        # ── 6. Lumped port on FeedSheet ───────────────────────────────────────
        # Use PyAEDT lumped_port() — handles Terminal Network terminal naming
        # automatically (Modal uses modes; Terminal uses terminal IDs).
        # Integration line runs across the wire diameter in X at z=0 (mid-gap).
        obound = h._odesign.GetModule("BoundarySetup")

        port = h.lumped_port(
            assignment="FeedSheet",
            name="Port1",
            impedance=50,
            integration_line=[[r_wire, 0, 0], [-r_wire, 0, 0]],
        )
        log(f"lumped_port() returned: {port}")

        # Verify via excitations (lumped ports are excitations, not boundaries)
        excitations = list(obound.GetExcitations())
        log(f"Excitations after lumped port: {excitations}")
        if not any("Port1" in e for e in excitations):
            raise RuntimeError(f"Port1 not found in excitations — lumped port creation failed. Got: {excitations}")
        log("Port1 (lumped, Terminal) OK")

        # ── 7. Solution setup ─────────────────────────────────────────────────
        setup = h.create_setup(name="Setup1")
        setup.props["Frequency"] = f"{freq_ghz}GHz"
        setup.props["MaximumPasses"] = 15
        setup.props["MaxDeltaS"] = 0.02
        setup.update()
        log("Setup1 OK")

        # ── 8. Frequency sweeps ───────────────────────────────────────────────
        # Sweep around resonance: freq ± 40 %
        sw_start = round(freq_ghz * 0.6, 3)
        sw_stop  = round(freq_ghz * 1.4, 3)
        sw_step  = round((sw_stop - sw_start) / 200, 4)

        h.create_linear_step_sweep(
            "Setup1", "GHz", sw_start, sw_stop, sw_step,
            name="Sweep1", sweep_type="Interpolating", save_fields=True,
        )
        log(f"Sweep1 ({sw_start}–{sw_stop} GHz) OK")

        # Radiation sweep (coarser step, save rad fields)
        sw2 = h.create_linear_step_sweep(
            "Setup1", "GHz", sw_start, sw_stop, round(sw_step * 5, 4),
            name="Sweep_Rad", sweep_type="Interpolating",
        )
        if sw2:
            sw2.props["SaveRadFields"] = True
            sw2.props["SaveFields"] = False
            sw2.update()
        log("Sweep_Rad OK")

        # ── 9. Far-field infinite sphere ──────────────────────────────────────
        h.insert_infinite_sphere(
            phi_start=0, phi_stop=360, phi_step=2,
            theta_start=0, theta_stop=180, theta_step=2,
            name="3D",
        )
        log("Infinite sphere '3D' inserted")

        # ── 10. Save & Solve ──────────────────────────────────────────────────
        h.save_project()
        log("Project saved.")
        log("Solving … (may take several minutes)")
        h.analyze_setup("Setup1")
        log("Solve complete.")

        # ── 11. Reports ───────────────────────────────────────────────────────
        excitations = list(obound.GetExcitations())
        log(f"Excitations after solve: {excitations}")
        # Terminal Network: terminal names are like "Port1_T1"
        # Step through pairs (name, type) if AEDT returns interleaved list
        terminal_names = [excitations[i] for i in range(0, len(excitations), 2)] if excitations else []
        terminal = next((e for e in terminal_names if "_T" in e),
                        next((e for e in excitations if "_T" in e), "Port1_T1"))
        s11_expr  = f"dB(S({terminal},{terminal}))"
        log(f"S11 expression: {s11_expr}")

        try:
            rep = h.post.reports_by_category.standard(
                expressions=s11_expr, setup="Setup1 : Sweep1")
            rep.create("S11_Report")
            log("S11_Report OK")
        except Exception as ex:
            log(f"  S11_Report: {ex}")

        try:
            rep_e = h.post.reports_by_category.far_field(
                expressions="dB(GainTotal)",
                setup="Setup1 : LastAdaptive",
                sphere_name="3D",
            )
            rep_e.primary_sweep = "Theta"
            rep_e.variations    = {"Phi": "90deg", "Freq": f"{freq_ghz}GHz"}
            rep_e.create("RadiationPattern_Phi90")
            log("E-plane (Phi=90°) OK")
        except Exception as ex:
            log(f"  E-plane: {ex}")

        try:
            rep_h = h.post.reports_by_category.far_field(
                expressions="dB(GainTotal)",
                setup="Setup1 : LastAdaptive",
                sphere_name="3D",
            )
            rep_h.primary_sweep = "Theta"
            rep_h.variations    = {"Phi": "0deg", "Freq": f"{freq_ghz}GHz"}
            rep_h.create("RadiationPattern_Phi0")
            log("H-plane (Phi=0°) OK")
        except Exception as ex:
            log(f"  H-plane: {ex}")

        try:
            h.post.create_report(
                expressions=["dB(GainTotal)"],
                setup_sweep_name="Setup1 : LastAdaptive",
                report_category="Far Fields",
                plot_type="3D Polar Plot",
                primary_sweep_variable="Theta",
                secondary_sweep_variable="Phi",
                context="3D",
                plot_name="RadiationPattern_3D",
                show=False,
            )
            log("3D Polar radiation pattern OK")
        except Exception as ex:
            log(f"  3D polar: {ex}")

        h.save_project()
        log("=== Complete! ===")
        _script_results[job_id] = {"status": "done", "output": "\n".join(output_lines)}

    except Exception as e:
        output_lines.append(f"FATAL ERROR: {e}")
        output_lines.append(_tb.format_exc())
        _script_results[job_id] = {"status": "error", "output": "\n".join(output_lines)}


@mcp.tool()
def create_438mhz_dipole(project: str = "Project1", port: int = 50051) -> str:
    """One-click recreation of the 438 MHz half-wave dipole antenna (HFSSDesign1).

    Reproduces the exact model from Project1 / HFSSDesign1:
      - Center-fed half-wave dipole at 438 MHz in free space
      - HFSS Terminal Network solution type
      - Two copper cylinder arms (171.2 mm each, 2 mm radius), 2 mm feed gap
      - Lumped port (Port1) at centre gap, 50 Ω reference
      - Radiation boundary on vacuum airbox
      - Setup1: adaptive at 438 MHz, 15 passes, ΔS=0.02
      - Sweep1: 262.8–613.2 MHz interpolating (save fields)
      - Sweep_Rad: coarser step, SaveRadFields=True
      - Infinite sphere '3D' (Phi 0–360°, Theta 0–180°, 2° step)
      - Reports: S11, E-plane (Phi=90°), H-plane (Phi=0°), 3D gain

    Runs in background — poll with get_script_result(job_id) every 30 s.

    Args:
        project: Project name to create the design in (default "Project1").
        port: gRPC port AEDT is listening on (default 50051).
    """
    global _script_counter
    err = _check_connection()
    if err:
        return err
    _script_counter += 1
    job_id = f"job_{_script_counter}"
    _script_results[job_id] = {"status": "running", "output": "Starting 438 MHz dipole workflow…"}
    t = threading.Thread(
        target=_build_dipole_antenna,
        args=(job_id, 0.438, "HFSSDesign1", project, port),
        daemon=True,
    )
    t.start()
    return _json({
        "ok": True,
        "job_id": job_id,
        "message": (
            f"438 MHz dipole workflow started (HFSSDesign1 in {project}). "
            f"Poll with get_script_result('{job_id}') every 30 s."
        ),
    })


@mcp.tool()
def design_dipole_antenna(
    freq_ghz: float = 0.438,
    design_name: str = "HFSSDesign1",
    project: str = "",
    port: int = 50051,
) -> str:
    """Design a complete center-fed half-wave dipole antenna in free space.

    Triggered by requests like:
      "design a 438 MHz dipole antenna — geometry, solution, solve,
       return loss and radiation pattern in 2D and 3D"

    Workflow (runs in background — poll with get_script_result):
      1. Create a NEW HFSS Modal design named <design_name>
      2. Compute half-wave dipole arm length (λ/4) for <freq_ghz>
      3. Build geometry: two copper cylinder arms + feed gap sheet + airbox
      4. Assign PEC to arms, radiation boundary to airbox
      5. Add lumped port (Port1) at centre feed gap
      6. Create Setup1 (adaptive at freq_ghz, 15 passes, ΔS=0.02)
      7. Add Sweep1 (±40 % around design frequency, interpolating)
      8. Add Sweep_Rad (coarser, SaveRadFields=True)
      9. Insert infinite sphere '3D' for far-field
     10. Solve
     11. Create reports: S11 return loss, E-plane, H-plane, 3D gain

    Args:
        freq_ghz: Design frequency in GHz (default 0.438 = 438 MHz).
        design_name: HFSS design name to create (default "HFSSDesign1").
        project: Project name (empty = active project).
        port: gRPC port AEDT is listening on (default 50051).
    """
    global _script_counter
    err = _check_connection()
    if err:
        return err
    _script_counter += 1
    job_id = f"job_{_script_counter}"
    _script_results[job_id] = {"status": "running", "output": "Starting dipole antenna workflow…"}
    t = threading.Thread(
        target=_build_dipole_antenna,
        args=(job_id, freq_ghz, design_name, project, port),
        daemon=True,
    )
    t.start()
    return _json({
        "ok": True,
        "job_id": job_id,
        "message": (
            f"Dipole antenna workflow started for {freq_ghz*1000:.0f} MHz. "
            f"Poll with get_script_result('{job_id}') every 30 s — "
            "solve takes several minutes."
        ),
    })


# ---------------------------------------------------------------------------
# Scripting fallback
# ---------------------------------------------------------------------------

_script_results: dict = {}   # job_id -> {"status": "running"|"done"|"error", "output": str}
_script_counter = 0


def _run_script_thread(job_id: str, script: str) -> None:
    import contextlib, io
    try:
        ns: dict = {"hfss": _hfss}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(script, ns)  # noqa: S102
        output = buf.getvalue().strip()
        _script_results[job_id] = {"status": "done", "output": output or "(no output)"}
    except Exception as e:
        _script_results[job_id] = {"status": "error", "output": str(e)}


@mcp.tool()
def run_hfss_script(script: str) -> str:
    """Execute arbitrary Python/PyAEDT code against the live HFSS session.

    Runs in the background to avoid MCP timeout. Returns a job_id immediately.
    Call get_script_result(job_id) to retrieve the output once done.

    The variable ``hfss`` is pre-bound to the active PyAEDT Hfss instance.
    Use ``print()`` to return data.

    Args:
        script: Python source code to execute.
    """
    global _script_counter
    err = _check_connection()
    if err:
        return err
    _script_counter += 1
    job_id = f"job_{_script_counter}"
    _script_results[job_id] = {"status": "running", "output": ""}
    t = threading.Thread(target=_run_script_thread, args=(job_id, script), daemon=True)
    t.start()
    return _json({"ok": True, "job_id": job_id, "message": f"Script running in background. Call get_script_result('{job_id}') to get output."})


@mcp.tool()
def get_script_result(job_id: str) -> str:
    """Get the output of a previously submitted run_hfss_script job.

    Args:
        job_id: The job_id returned by run_hfss_script.
    """
    if job_id not in _script_results:
        return _json({"ok": False, "error": f"Unknown job_id '{job_id}'."})
    result = _script_results[job_id]
    if result["status"] == "running":
        return _json({"ok": True, "status": "running", "message": "Still running — try again in a few seconds."})
    ok = result["status"] == "done"
    return _json({"ok": ok, "status": result["status"], "output": result["output"]})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
