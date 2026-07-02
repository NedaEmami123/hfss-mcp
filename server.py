from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hfss-mcp")

# Module-level handle so tools share one AEDT session across calls.
_hfss = None


@mcp.tool()
def say_hello(name: str) -> str:
    """Say hello to someone, to test that the MCP server is working."""
    return f"Hello, {name}! The server is working."


@mcp.tool()
def connect_to_hfss(
    project: str = "",
    design: str = "",
    new_desktop: bool = False,
    non_graphical: bool = False,
    version: str = "2026R1",
) -> str:
    """Connect to an existing AEDT/HFSS session or launch a new one.

    Parameters
    ----------
    project:
        Full path to an .aedt project file to open, or an empty string to use
        whatever project is already active in the running AEDT session.
    design:
        Name of the HFSS design to activate.  Leave empty to use the active
        design (or the first one found).
    new_desktop:
        Set to true to force-launch a brand-new AEDT process instead of
        attaching to an already-running one.
    non_graphical:
        Set to true to run AEDT without a visible GUI (batch / headless mode).
    version:
        AEDT version to target.  Accepted formats: ``"2026R1"``, ``"2026.1"``,
        ``"261"``.  Defaults to ``"2026R1"``.

    Returns
    -------
    str
        A confirmation string showing the AEDT version, project name, design
        name, and solution type — or an error message if the connection fails.
    """
    global _hfss

    try:
        from ansys.aedt.core import Hfss
    except ImportError as exc:
        return f"ERROR: Could not import ansys.aedt.core — {exc}"

    try:
        # Release any stale session first so we don't accumulate AEDT processes.
        if _hfss is not None:
            try:
                _hfss.release_desktop(close_projects=False, close_desktop=False)
            except Exception:
                pass
            _hfss = None

        _hfss = Hfss(
            project=project or None,
            design=design or None,
            version=version or None,
            new_desktop=new_desktop,
            non_graphical=non_graphical,
            close_on_exit=False,   # keep AEDT alive after this call returns
        )

        version = _hfss.aedt_version_id
        proj = _hfss.project_name
        des = _hfss.design_name
        sol = _hfss.solution_type

        return (
            f"Connected successfully.\n"
            f"  AEDT version : {version}\n"
            f"  Project      : {proj}\n"
            f"  Design       : {des}\n"
            f"  Solution type: {sol}"
        )

    except Exception as exc:
        return f"ERROR connecting to HFSS: {type(exc).__name__}: {exc}"


if __name__ == "__main__":
    mcp.run()
