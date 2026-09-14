"""
extract_cic.py — Workspace Code-Intelligence Contract (CIC) Blueprint extractor

D-20260906-001 Phase 6 (pre-registered plan: .agents/project/org/decisions/_drafts/
2026-09-06-hmm-cic-brain-logger.md)

Static, two-stage code introspection over the workspace that produces a compact,
LLM-consumable blueprint (workspace_blueprint.json) so the Nemotron brain can
route tasks to capabilities without re-scanning the whole tree on every boot.

Stage A (AST pass): classes, public method signatures, module-level imports, first
docstring line, and executed module deps (token-bounded to control footprint).

Stage B (semantic pass): derives the CIC contract — module_category,
implementation_state, skeleton_structure (classes/methods), internal/external
dependencies, and the nemotron_interaction_contract (required_upstream_from_nemotron,
emits_downstream_to_nemotron, data_gaps_or_stubs).

Schema (frozen, one row per scanned python module):
    {
      "file_path": "Quantitative/company_classifier.py",
      "module_category": "screener" | "ingestion" | "risk_engine" | "execution" | "audit" | "other",
      "implementation_state": "greenfield_stub" | "partial" | "complete",
      "skeleton_structure": {
        "classes": [{"name": ..., "methods": [...], "inherits": [...], "docstring": "..."}],
        "module_functions": [...],
        "external_dependencies": [...],   # 3rd-party imports
        "internal_dependencies": [...]    # project-internal imports
      },
      "nemotron_interaction_contract": {
        "required_upstream_from_nemotron": [...],
        "emits_downstream_to_nemotron": [...],
        "data_gaps_or_stubs": [...]
      }
    }

Failure discipline: AST extraction must not OOM on the scanned tree; a module that
raises during introspection is recorded with implementation_state="partial" and a
diagnostic in data_gaps_or_stubs — the run never aborts. Success bar: >95% of
scanned modules extract cleanly, blueprint footprint 3–5k tokens.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import sys
import tokenize
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Scratch modules excluded from the blueprint (work scaffolding, not capabilities).
EXCLUDE_RE = re.compile(
    r"(__pycache__|\.git|\.cache|node_modules|/tests?/|test_|_test|venv|docs|tmp)"
)

# Lane/category classifier: file_name or module keywords -> category.
CATEGORY_KEYWORDS: List[tuple] = [
    ("screen", "screener"),
    ("screener", "screener"),
    ("valuation", "screener"),
    ("classifier", "screener"),
    ("scrape", "ingestion"),
    ("harvest", "ingestion"),
    ("fetcher", "ingestion"),
    ("source", "ingestion"),
    ("ingest", "ingestion"),
    ("fred", "ingestion"),
    ("cdxj", "ingestion"),
    ("risk", "risk_engine"),
    ("var", "risk_engine"),
    ("drawdown", "risk_engine"),
    ("regime", "risk_engine"),
    ("macro", "risk_engine"),
    ("shock", "risk_engine"),
    ("shield", "risk_engine"),
    ("monitor", "risk_engine"),
    ("sensitivity", "risk_engine"),
    ("liquidity", "risk_engine"),
    ("corrosion", "audit"),
    ("provenance", "audit"),
    ("audit", "audit"),
    ("gatekeeper", "audit"),
    ("order", "execution"),
    ("allocation", "execution"),
    ("execution", "execution"),
    ("rebalance", "execution"),
]

# Heuristic: how complete a module looks, based on the body's real work.
_STUB_MARKERS: List[str] = ["pass", "NotImplementedError", "# TODO"]
_WORD_PASS = re.compile(r"\bpass\b")


def classify_category(file_path: Path) -> str:
    parts = file_path.as_posix().split("/")
    for kw, cat in CATEGORY_KEYWORDS:
        if any(kw in p.lower() for p in parts):
            return cat
    return "other"


def _count_bodies(node: ast.AST) -> int:
    """Count how many concrete statements (real work) a function/class body has."""
    bodies = list(getattr(node, "body", []))
    if any(isinstance(b, ast.If) and b.test and isinstance(b.test, ast.Constant) and b.test.value in (True, False)
           for b in bodies):
        bodies = [b for b in bodies if not isinstance(b, ast.If)]
    return len(bodies)


def cast_body(node: ast.AST) -> list:
    return list(getattr(node, "body", []) or [])


def _doc_first_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return ""
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return first[:60]


def _is_stub_like(node: ast.AST) -> bool:
    """A class is stub-like only if its body has no real statements — just a
    docstring and/or a pass. (Except-handler ``pass`` in working code is not a
    stub; only body-level pas stubs are.)"""
    body = cast_body(node)
    return (not body) or all(
        isinstance(b, (ast.Pass, ast.Expr))
        and not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Str))
        for b in body
    )


def _is_wrapper(node: ast.FunctionDef) -> bool:
    """Only genuinely empty/'pass'-only bodies count as thin wrappers.

    A single ``return <<property>>`` (dataclass-ish accessor) is real code, not
    a stub — avoids false positives on property methods.
    """
    body = [b for b in getattr(node, "body", [])]
    if not body:
        return True
    if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
        return False
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return True
    # Multi-statement bodies are real work unless only pass/raise stubs remain.
    has_real = any(
        isinstance(b, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Call, ast.Return, ast.For,
                       ast.While, ast.If, ast.With, ast.Try, ast.Raise))
        for b in body
        if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Str))
    )
    return not has_real


def _implemented_funcs(cls_ast: ast.ClassDef) -> List[str]:
    return [
        n.name for n in cls_ast.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_wrapper(n)
    ]


_CURRENT_TREE: Optional[ast.AST] = None
_CURRENT_SOURCE: Optional[str] = None


def _scan_ast(module_file: Path) -> Dict[str, Any]:
    with tokenize.open(module_file) as fh:
        src = fh.read()
    tree = ast.parse(src, filename=str(module_file))
    global _CURRENT_TREE, _CURRENT_SOURCE
    _CURRENT_TREE = tree
    _CURRENT_SOURCE = src

    imports_external: Set[str] = set()
    imports_internal: Set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for alias in n.names:
                top = alias.name.split(".")[0]
                (imports_internal if top in ("Quantitative", "valuation_alpha", "backtesting",
                                             "config", "center", "diversification", "optimization",
                                             "dashboard", "discovery", "portfolio")
                 else imports_external).add(alias.name)
        elif isinstance(n, ast.ImportFrom):
            if n.module and (n.module.split(".")[0] in ("Quantitative", "valuation_alpha",
                                                        "backtesting", "config", "center",
                                                        "diversification", "optimization", "dashboard",
                                                        "discovery", "portfolio")):
                imports_internal.add(n.module)
            elif n.module:
                imports_external.add(n.module)

    classes: List[Dict[str, Any]] = []
    module_funcs: List[str] = []
    stubs: List[str] = []
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            methods = []
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(m.name)
                    if _is_wrapper(m):
                        stubs.append(f"{n.name}.{m.name}:wrapper_or_pass")
            if _is_stub_like(n):
                stubs.append(f"{n.name}:pass_or_notimplemented")
            classes.append({
                "name": n.name,
                "methods": methods,
                "inherits": [b.id for b in n.bases if isinstance(b, ast.Name)],
                "docstring": _doc_first_line(n),
            })
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_funcs.append(n.name)
            if _is_wrapper(n):
                stubs.append(f"{n.name}:wrapper_or_pass")

    implemented = any(c["methods"] or c["docstring"] for c in classes) or bool(module_funcs)
    raise_count = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Raise) and
                      getattr(n, "exc", None) is not None and isinstance(n.exc, ast.Call) and
                      isinstance(n.exc.func, ast.Name) and
                      n.exc.func.id in ("NotImplementedError", "RuntimeError"))

    if not implemented and (stubs or raise_count):
        state = "greenfield_stub"
    elif any(s.endswith(":pass_or_notimplemented") for s in stubs) or raise_count >= 3:
        state = "partial"
    elif not stubs and implemented:
        state = "complete"
    else:
        state = "partial"

    return {
        "classes": classes,
        "module_functions": module_funcs,
        "external": sorted(imports_external),
        "internal": sorted(imports_internal),
        "stubs": stubs,
        "state": state,
        "node_count": len(list(ast.walk(tree))),
    }


def _semantic_contract(module_file: Path, scan: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the CIC contract from (a) the AST pass and (b) static naming heuristics."""
    cat = classify_category(module_file)
    stubs = scan["stubs"]

    required_upstream: List[str] = []
    emits_downstream: List[str] = []

    # Standardized upstream/downstream contract per category (frozen vocabulary).
    if cat == "ingestion":
        required_upstream = ["nemotron:schedule_source_refresh", "nemotron:source_health_policy"]
        emits_downstream = ["data:raw_snapshot", "audit:ingestion_provenance"]
    elif cat == "screener":
        required_upstream = ["data:raw_snapshot", "nemotron:universe_filter"]
        emits_downstream = ["data:candidate_universe", "audit:screen_run"]
    elif cat == "risk_engine":
        required_upstream = ["data:candidate_universe", "nemotron:risk_tolerance"]
        emits_downstream = ["data:risk_adjustments", "audit:risk_rules"]
    elif cat == "execution":
        required_upstream = ["data:risk_adjustments", "nemotron:execution_policy"]
        emits_downstream = ["data:orders", "audit:execution_ledger"]
    elif cat == "audit":
        required_upstream = ["data:*", "nemotron:audit_policy"]
        emits_downstream = ["audit:report", "audit:provenance_hash"]
    else:
        required_upstream = ["nemotron:task_spec"]
        emits_downstream = ["data:derived_product"]

    # data gaps: modules that only declare methods but the AST found no concrete body
    # (greenfield_stub) or heavy raise usage are flagged as gaps for the brain.
    gaps: List[str] = []
    for s in stubs:
        gaps.append(f"stub:{s}")
    if cat == "ingestion" and not scan["module_functions"] and not any(
        c["methods"] for c in scan["classes"]
    ):
        gaps.append("no_ingest_action_found")

    if scan["state"] == "greenfield_stub":
        gaps.append("greenfield:needs_full_implementation")

    return {
        "required_upstream_from_nemotron": required_upstream,
        "emits_downstream_to_nemotron": emits_downstream,
        "data_gaps_or_stubs": sorted(set(gaps)),
    }


def _collect_py_files(root: Path) -> List[Path]:
    out: List[Path] = []
    for p in sorted(root.rglob("*.py")):
        if EXCLUDE_RE.search(p.as_posix()):
            continue
        if p.name == "__init__.py":
            continue
        out.append(p)
    return out


def extract_blueprint(
    root: Optional[Path] = None,
    output_path: Optional[Path] = None,
    college_log: bool = True,
) -> Dict[str, Any]:
    """Run the two-stage CIC extraction and write workspace_blueprint.json."""
    root = (root or REPO_ROOT).resolve()
    out_path = output_path or (REPO_ROOT / "data" / "pipeline_cache" / "workspace_blueprint.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    files = _collect_py_files(root)
    rows: List[Dict[str, Any]] = []
    failures = 0
    for f in files:
        rel = f.relative_to(root).as_posix()
        try:
            scan = _scan_ast(f)
            contract = _semantic_contract(f, scan)
            row = {
                "file_path": rel,
                "module_category": classify_category(f),
                "implementation_state": scan["state"],
                "skeleton_structure": {
                    "classes": scan["classes"],
                    "module_functions": scan["module_functions"],
                    "external_dependencies": scan["external"],
                    "internal_dependencies": scan["internal"],
                },
                "nemotron_interaction_contract": contract,
            }
            rows.append(row)
            if college_log:
                try:
                    from config.logging_config import college_event
                    college_event(
                        "cic_extracted",
                        file=rel,
                        category=row["module_category"],
                        state=row["implementation_state"],
                        classes=len(row["skeleton_structure"]["classes"]),
                        stubs=len(row["nemotron_interaction_contract"]["data_gaps_or_stubs"]),
                    )
                except Exception:
                    pass
        except Exception as exc:  # never let one module abort the extract
            failures += 1
            rows.append({
                "file_path": rel,
                "module_category": classify_category(f),
                "implementation_state": "partial",
                "skeleton_structure": {"classes": [], "module_functions": [], "external_dependencies": [], "internal_dependencies": []},
                "nemotron_interaction_contract": {
                    "required_upstream_from_nemotron": [],
                    "emits_downstream_to_nemotron": [],
                    "data_gaps_or_stubs": [f"extract_error:{type(exc).__name__}:{exc}"],
                },
            })
            logger.warning("CIC extract failed on %s: %s", rel, exc)

    blueprint = {
        "schema_version": "1.0",
        "generated_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "root": str(root),
        "module_count": len(rows),
        "extract_failures": failures,
        "modules": rows,
    }

    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(blueprint, fh, indent=2, default=str)

    routing = project_routing(blueprint)
    routing_path = out_path.with_name("workspace_routing.json")
    with routing_path.open("w", encoding="utf-8") as fh:
        json.dump(routing, fh, indent=2, default=str)

    if college_log:
        try:
            from config.logging_config import college_event
            college_event(
                "cic_blueprint_written",
                path=str(out_path.resolve()),
                modules=len(rows),
                failures=failures,
                est_tokens=estimate_tokens(blueprint),
                routing_tokens=estimate_tokens(routing),
                routing_path=str(routing_path.resolve()),
            )
        except Exception:
            pass

    return blueprint


def project_routing(blueprint: Dict[str, Any]) -> Dict[str, Any]:
    """Compact boot-time projection for the Nemotron brain.

    The full blueprint (workspace_blueprint.json) is the schema-faithful CIC
    artifact; the routing projection keeps only what the brain needs to route a
    task: module path -> [cat_code+state_code, data_gaps]. Category/state use
    one-char codes to stay inside the 3-5k-token boot budget.
    """
    cat_codes = {"screener": "s", "ingestion": "i", "risk_engine": "r", "execution": "e", "audit": "a"}
    state_codes = {"greenfield_stub": "g", "partial": "p", "complete": "c"}
    mods = {}
    for m in blueprint.get("modules", []):
        cat = m.get("module_category", "other")
        if cat not in cat_codes:  # brain only routes the five operational lanes
            continue
        code = cat_codes[cat] + state_codes.get(m.get("implementation_state", "partial"), "p")
        gaps = m.get("nemotron_interaction_contract", {}).get("data_gaps_or_stubs", [])
        mods[m.get("file_path", "?")] = [code] + gaps
    return {
        "schema_version": blueprint.get("schema_version", "1.0"),
        "generated_utc": blueprint.get("generated_utc", ""),
        "module_count": len(mods),
        "categories": {v: k for k, v in cat_codes.items()},
        "states": {v: k for k, v in state_codes.items()},
        "modules": mods,
    }


def estimate_tokens(blueprint: Dict[str, Any]) -> int:
    """Rough token estimate: ~4 chars/token on the compact JSON footprint."""
    text = json.dumps(blueprint, default=str)
    return len(text) // 4


def load_blueprint(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or (REPO_ROOT / "data" / "pipeline_cache" / "workspace_blueprint.json")
    if not p.exists():
        return {"schema_version": "1.0", "module_count": 0, "modules": []}
    with p.open(encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging(level="INFO")
    bp = extract_blueprint()
    print(f"modules={bp['module_count']} failures={bp['extract_failures']} "
          f"est_tokens={estimate_tokens(bp)}")
    by_state: Dict[str, int] = {}
    for m in bp["modules"]:
        by_state[m["implementation_state"]] = by_state.get(m["implementation_state"], 0) + 1
    print("by_state:", by_state)
    print("written:", "data/pipeline_cache/workspace_blueprint.json")