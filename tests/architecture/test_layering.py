"""The layering rules, checked mechanically (Phase 13 architecture audit).

CLAUDE.md calls these rules "review-blocking", which for eleven phases meant a person reading a
diff. That works until it does not: the rules are exactly the kind that stay true by accident for a
while and then quietly stop — a repository that commits, a router that runs a query, a module that
reaches into another's private helpers. None of those break a test, and all of them are expensive to
undo once something has been built on top.

So the audit is a test rather than a document. Each check below is one rule from CLAUDE.md, applied
to every file in ``src/``, with its deliberate exceptions written down as code rather than
remembered. A new module gets the rules for free; a violation fails on the PR that introduces it.

Where a rule has an exception, the exception is named and justified here. That is the honest form of
an audit: not "no deviations", but "these deviations, for these reasons".
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from httpx import AsyncClient

SRC = Path(__file__).resolve().parents[2] / "src"
MODULES = SRC / "modules"


def module_of(path: Path) -> str:
    """The top-level feature module a file belongs to, e.g. ``agents``."""
    return path.relative_to(MODULES).parts[0]


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def imports_in(path: Path) -> list[str]:
    """Every dotted module name this file imports, including ``from`` targets."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


# -- module shape --------------------------------------------------------------------


def test_every_module_has_the_domain_internal_presentation_shape() -> None:
    """A module that grows a fourth top-level package is a module that has stopped being one."""
    allowed = {"domain", "internal", "presentation", "__init__.py", "__pycache__"}
    # `channels` carries two channel sub-modules beside its own layers (spec §5.5): the web and
    # WhatsApp transports share one internal message format and one set of channel settings, and
    # splitting them into sibling top-level modules would duplicate both.
    allowed_extra = {"channels": {"web", "whatsapp"}}

    unexpected: list[str] = []
    for module in sorted(p for p in MODULES.iterdir() if p.is_dir() and p.name != "__pycache__"):
        permitted = allowed | allowed_extra.get(module.name, set())
        unexpected.extend(
            f"{module.name}/{entry.name}"
            for entry in module.iterdir()
            if entry.name not in permitted
        )

    assert unexpected == []


def test_every_module_package_is_importable() -> None:
    """A package without ``__init__.py`` imports by accident on some layouts and not others."""
    missing = [
        str(directory.relative_to(SRC))
        for directory in MODULES.rglob("*")
        if directory.is_dir()
        and directory.name != "__pycache__"
        and not (directory / "__init__.py").exists()
    ]

    assert missing == []


# -- repositories --------------------------------------------------------------------


def test_no_repository_commits() -> None:
    """The session dependency owns the transaction boundary, so repositories only ``flush()``.

    A repository that commits makes every caller's transaction end at a moment the caller did not
    choose — which is invisible until the second write in the same request needs to roll back.
    """
    offenders = [
        str(path.relative_to(SRC))
        for path in python_files(SRC)
        if path.name == "repositories.py" and "commit()" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_only_repositories_and_the_shared_base_build_queries() -> None:
    """``select(...)`` belongs in a repository.

    Services orchestrate and routers translate; a query in either is a tenant filter that can be
    forgotten in a place nobody thinks to look for one (spec §5.7).
    """
    # Machinery that is not feature data access, and is a query by nature:
    #   * `shared/database` is the repository base itself;
    #   * the modules' `internal/tasks.py` run in a worker with no request and no service around
    #     them, and are the documented place where a sweep asks the database what is due;
    #   * `channels/whatsapp/domain/repositories.py` exposes a module-level helper, which is still
    #     a repository — it is matched by name below, not by this list.
    allowed_suffixes = ("repositories.py", "internal/tasks.py")

    offenders = []
    for path in python_files(SRC):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith("shared/database/") or relative.endswith(allowed_suffixes):
            continue
        # Parsed rather than grepped: half the modules in this codebase discuss `select(...)` in a
        # docstring, and a text search would report every one of them.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "select"
            for node in ast.walk(tree)
        ):
            offenders.append(relative)

    assert offenders == []


# -- routers -------------------------------------------------------------------------


def controllers() -> list[Path]:
    return [path for path in python_files(MODULES) if "presentation/api" in path.as_posix()]


def test_routers_touch_no_repository_and_no_orm() -> None:
    """Routers parse, call one service, and wrap the result."""
    offenders = []
    for path in controllers():
        for imported in imports_in(path):
            if imported.startswith("sqlalchemy") or imported.endswith("domain.repositories"):
                offenders.append(f"{path.relative_to(SRC).as_posix()} imports {imported}")

    assert offenders == []


def test_routers_are_built_with_create_router() -> None:
    """``create_router`` is what applies the envelope's ``by_alias`` / ``exclude_none`` rules.

    A bare ``APIRouter`` still works, which is the problem: the endpoint returns almost the right
    JSON, with nulls where the contract says a field is absent.
    """
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in controllers()
        if "APIRouter(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


# -- module boundaries ---------------------------------------------------------------


def test_no_module_imports_another_modules_internal() -> None:
    """``internal/`` is module-private. Reaching into one couples to a decision, not a contract."""
    offenders = []
    for path in python_files(MODULES):
        owner = module_of(path)
        for imported in imports_in(path):
            match = re.match(r"src\.modules\.([a-z_]+)\..*internal", imported)
            if match and match.group(1) != owner:
                offenders.append(f"{path.relative_to(SRC).as_posix()} imports {imported}")

    assert offenders == []


def test_cross_module_access_goes_service_to_service() -> None:
    """No module reaches into another module's repositories.

    Models may be imported across modules — a service needs the type of what it is handed — but a
    repository is the other module's data access, and using one bypasses every rule that module
    enforces about its own rows.

    **The exception is `analytics`,** which is a read model over the other modules' tables and says
    so (`analytics/domain/repositories.py`). Its repositories are its own; they read other modules'
    *models*, always through a tenant-scoped base, which is the isolation guarantee restated rather
    than reimplemented.
    """
    offenders = []
    for path in python_files(MODULES):
        owner = module_of(path)
        for imported in imports_in(path):
            match = re.match(r"src\.modules\.([a-z_]+)\.domain\.repositories", imported)
            if match and match.group(1) != owner:
                offenders.append(f"{path.relative_to(SRC).as_posix()} imports {imported}")

    assert offenders == []


def test_models_inherit_the_shared_base() -> None:
    """Every table gets a UUID id and timestamps, and tenant-owned tables get ``tenant_id``."""
    bases = {"BaseModel", "TenantScopedModel"}
    offenders = []

    for path in python_files(MODULES):
        if path.name != "models.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            names = {base.id for base in node.bases if isinstance(base, ast.Name)}
            # Enums and dataclasses in a models module are not tables.
            if names & {"Enum", "str"} or not names:
                continue
            if not names & bases:
                offenders.append(f"{path.relative_to(SRC).as_posix()}::{node.name}")

    assert offenders == []


# -- the response envelope -----------------------------------------------------------


async def test_every_successful_response_is_the_envelope(client: AsyncClient) -> None:
    """No endpoint returns a bare dict or a naked model.

    Checked against the published schema rather than the source, because the schema is what a
    caller integrates against — and it is the only place a ``response_model`` that was quietly
    omitted becomes visible.
    """
    schema = (await client.get("/openapi.json")).json()

    # Server-sent events and Meta's webhook handshake are the two routes that cannot carry the
    # envelope: one is a stream of `text/event-stream` frames, the other must answer with the bare
    # challenge string Meta sent or the connection is refused. Both are documented as such.
    exempt = {
        ("/v1/chat/messages/stream", "post"),
        ("/v1/channels/whatsapp/webhook/{connection_id}", "get"),
    }

    offenders = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if (path, method) in exempt:
                continue
            content = (
                operation.get("responses", {})
                .get("200", operation.get("responses", {}).get("201", {}))
                .get("content", {})
                .get("application/json", {})
            )
            reference = str(content.get("schema", {}).get("$ref", ""))
            if not reference:
                offenders.append(f"{method.upper()} {path}: no JSON response model")
            elif not re.search(r"(ApiResponse|PaginatedResponse)", reference):
                offenders.append(f"{method.upper()} {path}: {reference}")

    assert offenders == []
