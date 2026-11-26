"""The two servers run without the bot: what ecobot-mcp and irrigation-mcp import, by any route (a module's imports anywhere in it,
function-local ones and the dynamic ones named in DYNAMIC), never reaches the bot's modules, Telegram or the model client, and
importing their entry points in a fresh interpreter loads none of them."""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = ("ecobot_mcp.__main__", "ecobot_mcp.core", "irrigation_mcp.__main__", "lib.mcp_server", "lib.irrigation.server")
FORBIDDEN = ("lib.bot", "lib.llm", "lib.prompt", "lib.intent", "lib.model_tools", "lib.weather", "lib.irrigation_remote",
             "lib.irrigation_menu", "lib.buttons", "lib.entities", "lib.admins", "lib.templates", "lib.periods", "lib.alerts.notify",
             "lib.alerts.menu", "telegram", "openai")
SERVICE_TOOLS = ("current", "summary", "readings", "days", "relate", "scan", "outlook", "forecast", "pollen", "about")
# The modules that import others by name at run time, and the modules they import that way
DYNAMIC = {"lib.service": [f"lib.service.{name}" for name in SERVICE_TOOLS],
           "lib.irrigation": ["lib.irrigation.tuya", "lib.irrigation.watchdog", "lib.irrigation.common"],
           "lib.chart_images": ["lib.charts"]}


def is_forbidden(name: str) -> bool:
    return any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN)


def file_of(module: str) -> Path | None:
    """The source file of a module of this repo (a package's __init__.py); None for any other module."""
    base = ROOT.joinpath(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    return base / "__init__.py" if (base / "__init__.py").is_file() else None


def imports_of(module: str) -> set[str]:
    """Every module named by an import statement anywhere in `module`'s source (relative ones resolved), and the submodules a
    `from x import y` may name."""
    path = file_of(module)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[:len(package.split(".")) - node.level + 1]
                base = ".".join(parts + ([node.module] if node.module else []))
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def reach() -> dict[str, str]:
    """{every module the entry points reach: the module that imports it first}."""
    seen, queue = {}, [(entry, "(entry)") for entry in ENTRIES]
    while queue:
        module, by = queue.pop()
        if module in seen:
            continue
        seen[module] = by
        if is_forbidden(module) or file_of(module) is None:
            continue
        parents = [".".join(module.split(".")[:i]) for i in range(1, len(module.split(".")))]
        queue.extend((name, module) for name in [*parents, *DYNAMIC.get(module, ()), *imports_of(module)])
    return seen


def test_the_servers_import_graph_reaches_nothing_of_the_bot():
    seen = reach()
    reached = {m: by for m, by in seen.items() if is_forbidden(m)}
    assert not reached, f"reached from the servers (module: imported by): {reached}"
    assert {"lib.alerts.pictures", "lib.service.summary", "lib.charts", "lib.irrigation.watchdog", "lib.upstream"} <= set(seen)


def test_every_module_that_imports_by_name_is_listed_in_DYNAMIC():
    named = {path.relative_to(ROOT).with_suffix("") for folder in ("lib", "ecobot_mcp", "irrigation_mcp") for path in (ROOT / folder).rglob("*.py")
             if "import_module(" in path.read_text() or "__import__(" in path.read_text()}
    modules = {".".join(p.parts[:-1] if p.name == "__init__" else p.parts) for p in named}
    assert modules <= set(DYNAMIC) | {"tests"}, modules


def test_importing_the_entry_points_loads_none_of_the_bot():
    code = ("import sys; import " + ", ".join(ENTRIES) + "; "
            "from lib.service import Service; import lib.chart_images, lib.irrigation; "
            "from lib.irrigation import Tuya, Watchdog, IrrigationError; "
            f"bad = [m for m in sys.modules if any(m == b or m.startswith(b + '.') for b in {FORBIDDEN!r})]; "
            "print(sorted(bad)); sys.exit(1 if bad else 0)")
    run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
