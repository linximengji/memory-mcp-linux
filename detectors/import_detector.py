"""Detect intra-project imports and pip editable dependencies.

Python imports use ast module for precise resolution.
TypeScript imports use regex with file-path resolution.
"""

import ast
import re
from pathlib import Path

# Match import statements with named imports.
# Groups: 1=named-content, 2=default-only, 3=bare-default
# Source path is always in the last group (group 4 for named, group varies per alternation)
# We extract the source path from after 'from' keyword via the always-present from-group.
TS_IMPORT_RE = re.compile(r"""
    import\s+
    (?:\{([^}]*)\}|default\s+(\w+)|(\w+)\s*,\s*\{([^}]*)\}|(\w+))
    \s+from\s+[\"']([^\"']+)[\"']
""", re.VERBOSE)

PIP_EDITABLE_RE = re.compile(r"""
    -e\s+
    (?:git\+https?://[^\s#]+|file://[^\s#]+|\.\.?/[^\s#]+)
""", re.VERBOSE | re.IGNORECASE)

PYTHON_SYMBOL_RE = re.compile(r"""
    ^(?:async\s+)?(?:def|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)
""", re.VERBOSE | re.MULTILINE)

TS_EXPORT_RE = re.compile(r"""
    ^export\s+(?:async\s+)?(?:function|class|interface|type|enum|default\s+(?:function|class))\s+
    ([a-zA-Z_$][a-zA-Z0-9_$]*)
""", re.VERBOSE | re.MULTILINE)

# Additional TS patterns for Phase 3
TS_EXPORT_CONST_RE = re.compile(r"""
    ^export\s+(?:const|let|var)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=
""", re.VERBOSE | re.MULTILINE)

TS_REEXPORT_RE = re.compile(r"""
    ^export\s+\{[^}]*\}\s+from\s+['"]([^'"]+)['"]
""", re.VERBOSE | re.MULTILINE)


# --- Python AST import parsing ---

def detect_imports_py(file_path: Path, content: str) -> list[dict]:
    """Extract Python import statements using ast module for precise parsing.

    Returns:
        List of import descriptors: [{type, source, symbols, line}]
    """
    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError:
        return []

    imports: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "type": "python",
                    "source": alias.name,
                    "symbols": [],
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # Skip relative imports (start with dot)
            if module.startswith("."):
                continue
            symbols = [alias.name for alias in node.names]
            imports.append({
                "type": "python",
                "source": module,
                "symbols": symbols,
                "line": node.lineno,
            })
    return imports


def detect_imports_ts(content: str) -> list[dict]:
    """Extract TypeScript/JS import paths and named symbols using regex."""
    imports: list[dict] = []
    for m in TS_IMPORT_RE.finditer(content):
        # Source path is always group 6 (from '...')
        source = m.group(6)
        if not source:
            continue

        # Parse imported symbols from whichever alternation matched
        symbols: list[str] = []
        if m.group(1):  # import { scan, json_decode } from './utils'
            symbols = [s.strip() for s in m.group(1).split(",") if s.strip()]
        elif m.group(2):  # import default from './foo'
            symbols.append(m.group(2))
        elif m.group(3) and m.group(4):  # import default, { named } from './foo'
            symbols.append(m.group(3))
            symbols.extend(s.strip() for s in m.group(4).split(",") if s.strip())
        elif m.group(5):  # import foo from './bar' (bare default)
            symbols.append(m.group(5))

        imports.append({
            "type": "typescript",
            "source": source,
            "symbols": symbols,
            "line": content[:m.start()].count("\n") + 1,
        })
    return imports


def detect_imports(file: Path, content: str) -> list[dict]:
    """Extract imports. Dispatches to language-specific parser.

    Returns:
        List of import descriptors.
    """
    ext = file.suffix.lower()
    if ext in (".py",):
        return detect_imports_py(file, content)
    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return detect_imports_ts(content)
    return []


# --- Import-to-edge resolution ---

def resolve_imports_to_edges(
    imports: list[dict],
    src_file: str,
    src_project: str,
    project_names: set[str],
    project_roots: dict[str, Path],
) -> list[dict]:
    """Resolve import descriptors into edge dicts with dst.file + dst.symbol populated.

    Handles:
      - Cross-project imports (module name matches a project name)
      - Intra-project imports (module resolves to a file in same project)
      - TS relative imports (./foo, ../bar)
      - Third-party imports (no edge emitted)
    """
    edges: list[dict] = []
    src_root = project_roots.get(src_project)
    if not src_root:
        return edges

    for imp in imports:
        source = imp["source"]
        imp_type = imp["type"]
        symbols = imp.get("symbols", [])

        if imp_type == "python":
            edges.extend(_resolve_py_import(source, src_file, src_project, src_root, project_names, project_roots, symbols))
        elif imp_type == "typescript":
            edges.extend(_resolve_ts_import(source, src_file, src_project, src_root, project_names, project_roots, symbols))

    return edges


def _resolve_py_import(
    module: str,
    src_file: str,
    src_project: str,
    src_root: Path,
    project_names: set[str],
    project_roots: dict[str, Path],
    symbols: list[str] | None = None,
) -> list[dict]:
    """Resolve a Python module path to a project file, one edge per symbol."""
    if not module:
        return []

    edges: list[dict] = []
    top = module.split(".")[0]
    symbols = symbols or []

    if top in project_names and top != src_project:
        dst_file = _find_py_module_walk(module, project_roots[top])
        dst_file_str = str(dst_file) if dst_file else ""
        if symbols:
            for sym_name in symbols:
                edges.append({
                    "src": {"project": src_project, "file": src_file},
                    "dst": {"project": top, "file": dst_file_str, "symbol": sym_name},
                    "kind": "import-dep",
                    "confidence": "extracted",
                    "detail": module,
                })
        else:
            edges.append({
                "src": {"project": src_project, "file": src_file},
                "dst": {"project": top, "file": dst_file_str},
                "kind": "import-dep",
                "confidence": "extracted",
                "detail": module,
            })
        return edges

    # Intra-project: walk full module path
    dst_file = _find_py_module_walk(module, src_root)
    if dst_file:
        if symbols:
            for sym_name in symbols:
                edges.append({
                    "src": {"project": src_project, "file": src_file},
                    "dst": {"project": src_project, "file": str(dst_file), "symbol": sym_name},
                    "kind": "import-dep",
                    "confidence": "extracted",
                    "detail": module,
                })
        else:
            edges.append({
                "src": {"project": src_project, "file": src_file},
                "dst": {"project": src_project, "file": str(dst_file)},
                "kind": "import-dep",
                "confidence": "extracted",
                "detail": module,
            })

    return edges


def _find_py_module_walk(module: str, root: Path) -> Path | None:
    """Resolve full dotted module path to file, walking each component.

    For 'x.y.z': tries R/x/y/z.py, R/x/y/z/__init__.py (exact path first),
    then falls back to R/x/y/__init__.py, R/x/__init__.py for shortest match.
    """
    parts = module.split(".")
    if not parts or not parts[0]:
        return None

    # Walk longest-to-shortest: full path first, then strip from end
    for depth in range(len(parts), 0, -1):
        sub = parts[:depth]
        # Try as .py file
        candidate = root.joinpath(*sub).with_suffix(".py")
        if candidate.exists():
            return candidate
        # Try as package __init__.py
        candidate = root.joinpath(*sub) / "__init__.py"
        if candidate.exists():
            return candidate

    return None


def _resolve_ts_import(
    imp_path: str,
    src_file: str,
    src_project: str,
    src_root: Path,
    project_names: set[str],
    project_roots: dict[str, Path],
    symbols: list[str] | None = None,
) -> list[dict]:
    """Resolve a TypeScript/JS import path to a project file, one edge per symbol."""
    edges: list[dict] = []
    symbols = symbols or []

    top = imp_path.split("/")[0]
    if top in project_names and top != src_project:
        if symbols:
            for sym_name in symbols:
                edges.append({
                    "src": {"project": src_project, "file": src_file},
                    "dst": {"project": top, "symbol": sym_name},
                    "kind": "import-dep",
                    "confidence": "extracted",
                    "detail": imp_path,
                })
        else:
            edges.append({
                "src": {"project": src_project, "file": src_file},
                "dst": {"project": top},
                "kind": "import-dep",
                "confidence": "extracted",
                "detail": imp_path,
            })
        return edges

    if imp_path.startswith("./") or imp_path.startswith("../"):
        src_dir = Path(src_file).parent
        resolved = (src_dir / imp_path).resolve()
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"):
            candidate = resolved.with_suffix(ext)
            if candidate.exists():
                target_proj = _infer_project(candidate)
                dst_proj = target_proj if target_proj else src_project
                if symbols:
                    for sym_name in symbols:
                        edges.append({
                            "src": {"project": src_project, "file": src_file},
                            "dst": {"project": dst_proj, "file": str(candidate), "symbol": sym_name},
                            "kind": "import-dep",
                            "confidence": "extracted",
                            "detail": imp_path,
                        })
                else:
                    edges.append({
                        "src": {"project": src_project, "file": src_file},
                        "dst": {"project": dst_proj, "file": str(candidate)},
                        "kind": "import-dep",
                        "confidence": "extracted",
                        "detail": imp_path,
                    })
                break
        else:
            edges.append({
                "src": {"project": src_project, "file": src_file},
                "dst": {"project": src_project},
                "kind": "import-dep",
                "confidence": "extracted",
                "detail": imp_path,
            })

    return edges


def _infer_project(path: Path) -> str | None:
    """Extract project name from an absolute path under WORKSPACE_ROOT."""
    from config import WORKSPACE_ROOT
    try:
        parts = path.absolute().parts
        ws_parts = WORKSPACE_ROOT.absolute().parts
        if len(parts) > len(ws_parts) and parts[:len(ws_parts)] == ws_parts:
            return parts[len(ws_parts)]
    except Exception:
        pass
    return None


# --- Symbol extraction ---

def extract_symbols_py(file_path: Path, content: str) -> list[dict]:
    """Extract Python symbols using ast for precise def/class/const detection.

    Returns:
        [{name, kind ('function'|'class'|'const'), file, line, decorators, docstring}]
    """
    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError:
        return []

    symbols: list[dict] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorators = []
            for d in node.decorator_list:
                if isinstance(d, ast.Name):
                    decorators.append(d.id)
                elif isinstance(d, ast.Attribute):
                    decorators.append(d.attr)
            symbols.append({
                "name": node.name,
                "kind": "function",
                "file": str(file_path),
                "line": node.lineno,
                "decorators": decorators,
                "docstring": ast.get_docstring(node) or "",
            })
        elif isinstance(node, ast.ClassDef):
            symbols.append({
                "name": node.name,
                "kind": "class",
                "file": str(file_path),
                "line": node.lineno,
                "decorators": [],
                "docstring": ast.get_docstring(node) or "",
            })
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    symbols.append({
                        "name": target.id,
                        "kind": "const",
                        "file": str(file_path),
                        "line": node.lineno,
                        "decorators": [],
                        "docstring": "",
                    })
    return symbols


def extract_symbols_ts(file_path: Path, content: str) -> list[dict]:
    """Extract TS/JS symbols using regex patterns.

    Covers: export function/class/interface/type/const, export const/let, re-exports.
    """
    symbols: list[dict] = []
    fpath = str(file_path)

    for m in TS_EXPORT_RE.finditer(content):
        line = content[:m.start()].count("\n") + 1
        symbols.append({
            "name": m.group(1),
            "kind": "export",
            "file": fpath,
            "line": line,
            "decorators": [],
            "docstring": "",
        })

    for m in TS_EXPORT_CONST_RE.finditer(content):
        line = content[:m.start()].count("\n") + 1
        symbols.append({
            "name": m.group(1),
            "kind": "export",
            "file": fpath,
            "line": line,
            "decorators": [],
            "docstring": "",
        })

    # Re-exports recorded as edges, not symbols (re-export source is external)
    return symbols


def extract_symbols(file: Path, content: str) -> list[dict]:
    """Extract exported/defined symbols. Dispatches to language-specific parser.

    Returns:
        List of symbol dicts: [{name, kind, file, line, decorators, docstring}]
    """
    ext = file.suffix.lower()
    if ext in (".py",):
        return extract_symbols_py(file, content)
    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return extract_symbols_ts(file, content)
    return []


def detect_pip_editable(project_root: Path) -> list[dict]:
    """Scan requirements files for pip editable installs referencing local projects.

    Returns:
        List of edge dicts.
    """
    edges: list[dict] = []
    for req_file in project_root.glob("*requirements*"):
        if not req_file.is_file():
            continue
        try:
            text = req_file.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in PIP_EDITABLE_RE.finditer(text):
            path = m.group(1)
            if path.startswith("."):
                resolved = (project_root / path).resolve()
                edges.append({
                    "src": {"project": project_root.name, "file": str(req_file)},
                    "dst": {"file": str(resolved)},
                    "kind": "pip-depends",
                    "confidence": "extracted",
                    "detail": path[:200],
                })
    return edges
