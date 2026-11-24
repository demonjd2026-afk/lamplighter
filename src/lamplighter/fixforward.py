"""Fix-forward brief for a bad release.

Always (no model needed): locate the culprit change by intersecting the failing
stack frame with the diff between the good and the bad release.

Optionally: ask a model for a patch, then treat that patch as untrusted. It
must pass a static screen, and it must pass replay in a sandbox (the requests
that failed must now be served, and healthy traffic must be reproduced exactly).
A patch that passes is "plausible", not "correct": passing tests is weaker than
being right, so it is written out for human review and never deployed here.
"""
from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path

from .actions import Actuator
from .config import Settings
from .llm import LLMClient, LLMError, propose_patch
from . import replay

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_SOURCE_BYTES = 32 * 1024
FORBIDDEN_NAMES = frozenset({"eval", "exec", "compile", "__import__", "open", "input", "globals", "locals",
                             "getattr", "setattr", "delattr", "vars", "breakpoint", "memoryview"})


def read_release_file(root: Path, service: str, release: str, filename: str) -> str | None:
    """Read one source file of a release, refusing anything outside the releases root."""
    if not all(NAME_RE.match(part) for part in (service, release)) or Path(filename).name != filename:
        return None
    base = root.resolve()
    path = (base / service / release / filename).resolve()
    if base not in path.parents or not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        return None
    return path.read_text(encoding="utf-8")


def culprit(good_source: str, bad_source: str, frame: dict | None, good_name: str, bad_name: str) -> dict:
    """Where the failing line sits relative to what the release changed."""
    good_lines, bad_lines = good_source.splitlines(), bad_source.splitlines()
    diff = list(difflib.unified_diff(good_lines, bad_lines, good_name, bad_name, lineterm="", n=2))
    result = {"diff": "\n".join(diff[:200]), "diff_truncated": len(diff) > 200, "frame": frame,
              "in_changed_code": False, "line_text": None, "hunk": None}
    if not frame:
        return result
    line = frame["line"]
    if 1 <= line <= len(bad_lines):
        result["line_text"] = bad_lines[line - 1].strip()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, good_lines, bad_lines, autojunk=False).get_opcodes():
        if tag != "equal" and j1 < line <= j2:
            result["in_changed_code"] = True
            result["hunk"] = {"start": j1 + 1, "end": j2, "text": "\n".join(bad_lines[j1:j2][:40])}
            break
    return result


def static_screen(new_source: str, old_source: str) -> list[str]:
    """Cheap screen of a model-written file. This reduces risk; it is not a sandbox."""
    problems: list[str] = []
    if len(new_source.encode()) > MAX_SOURCE_BYTES:
        return ["file too large"]
    try:
        new_tree, old_tree = ast.parse(new_source), ast.parse(old_source)
    except SyntaxError as exc:
        return [f"does not parse: {exc.msg}"]

    def imports(tree: ast.AST) -> set[str]:
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add(node.module or ".")
        return found

    def top_level(tree: ast.Module) -> set[str]:
        return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}

    added = imports(new_tree) - imports(old_tree)
    if added:
        problems.append(f"adds imports: {sorted(added)}")
    missing = top_level(old_tree) - top_level(new_tree)
    if missing:
        problems.append(f"removes definitions: {sorted(missing)}")
    for node in ast.walk(new_tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"uses {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"touches {node.attr}")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await)):
            problems.append(f"uses {node.__class__.__name__}")
    return sorted(set(problems))


def build(settings: Settings, plan: dict, signature: dict | None, cases: dict, actuator: Actuator,
          client: LLMClient | None) -> dict | None:
    """Build the brief for a rollback plan. Returns None when release sources are not available."""
    if plan.get("kind") != "rollback_release" or settings.releases_root is None:
        return None
    frame = (signature or {}).get("frame")
    filename = frame["file"] if frame else "logic.py"
    service, bad, good = plan["service"], str(plan["from"]), str(plan["to"])
    bad_source = read_release_file(settings.releases_root, service, bad, filename)
    good_source = read_release_file(settings.releases_root, service, good, filename)
    if bad_source is None or good_source is None:
        return None
    brief = {"service": service, "bad_release": bad, "good_release": good, "file": filename,
             "culprit": culprit(good_source, bad_source, frame, f"{good}/{filename}", f"{bad}/{filename}"),
             "patch": {"status": "not_attempted", "reason": "model patching is disabled"}}
    if client is None or not settings.llm_patch:
        return brief
    try:
        candidate = propose_patch(client, bad_source, good_source, (signature or {}).get("signature", ""), frame)
    except LLMError as exc:
        brief["patch"] = {"status": "not_attempted", "reason": str(exc)}
        return brief
    problems = static_screen(candidate, bad_source)
    if candidate == bad_source:
        problems.append("identical to the failing file")
    if problems:
        brief["patch"] = {"status": "rejected", "reason": "failed the static screen", "problems": problems}
        return brief
    sandbox_id = None
    try:
        sandbox = actuator.sandbox_create({service: bad}, {}, {filename: candidate})
        sandbox_id = sandbox["id"]
        result = replay.run(sandbox["checkout_url"], cases, settings.noise_keys, settings.allowed_hosts)
    except Exception as exc:  # noqa: BLE001 - a patch that cannot even start is simply rejected
        brief["patch"] = {"status": "rejected", "reason": f"could not be run in the sandbox: {exc.__class__.__name__}"}
        return brief
    finally:
        if sandbox_id is not None:
            try:
                actuator.sandbox_delete(sandbox_id)
            except Exception:  # noqa: BLE001
                pass
    diff = "\n".join(difflib.unified_diff(bad_source.splitlines(), candidate.splitlines(),
                                          f"a/{filename}", f"b/{filename}", lineterm=""))
    if result["passed"]:
        brief["patch"] = {"status": "plausible", "replay": result, "diff": diff,
                          "reason": "passes replay in a sandbox; passing replay is not proof of correctness, "
                                    "so this needs human review before it is merged"}
    else:
        brief["patch"] = {"status": "rejected", "reason": "does not pass replay", "replay": result}
    return brief
