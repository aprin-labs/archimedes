"""react and react-dom must ship at the same version (the 2026-09-25 blank site).

From 2026-09-25 to 2026-10-01 https://archimedes-arc.com rendered a blank
page. Dependabot PR #1872 bumped ``react`` (and ``@types/react``) to 19.3.0
and left ``react-dom`` at 19.2.8. npm accepted that: react-dom 19.2.8 declares
``peerDependencies: {"react": "^19.2.8"}``, which 19.3.0 satisfies. React does
not: react-dom compares its own version with react's at module load and throws
React error #527 on ANY difference, so the bundle died before mounting
anything into ``#root``. /health stayed green the whole time because nothing
in CI or the deploy executed the frontend bundle. PR #1905 restored the pair.

npm's peer-range check is exactly the check that let this through, so this
test asserts the stronger invariant React actually enforces, in BOTH files a
bump touches:

* ``ui/package.json`` declares the same range for react and react-dom, and
  ranges for @types/react and @types/react-dom on the same major.minor;
* ``ui/package-lock.json`` (what ``npm ci`` installs and Vite bundles)
  resolves every copy of react and react-dom in the tree to ONE version, and
  @types/react and @types/react-dom to the same major.minor.

Hermetic: reads two JSON files, no npm, no network. The ``TestTheCheckRejects``
cases feed the checker the exact incident versions so the guard is shown to
fail on the input it exists to reject, not just to pass on today's tree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_DIR = REPO_ROOT / "ui"
DEPENDABOT_YML = REPO_ROOT / ".github" / "dependabot.yml"

RUNTIME_PAIR = ("react", "react-dom")
TYPES_PAIR = ("@types/react", "@types/react-dom")


def _major_minor(version_or_range: str) -> tuple[int, int] | None:
    """``"^19.2.5"`` -> ``(19, 2)``; ``None`` when there is no X.Y to read."""
    match = re.search(r"(\d+)\.(\d+)", version_or_range)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _declared(manifest: dict, name: str) -> str | None:
    for section in ("dependencies", "devDependencies"):
        if name in manifest.get(section, {}):
            return manifest[section][name]
    return None


def _declared_problems(manifest: dict, where: str) -> list[str]:
    problems: list[str] = []
    react, react_dom = (_declared(manifest, n) for n in RUNTIME_PAIR)
    if react is None or react_dom is None:
        problems.append(f"{where}: react={react!r} react-dom={react_dom!r} (both must be declared)")
    elif react != react_dom:
        problems.append(
            f"{where}: react is {react!r} but react-dom is {react_dom!r} — bump them together "
            "(react-dom throws React error #527 on any version difference)"
        )
    types_react, types_react_dom = (_declared(manifest, n) for n in TYPES_PAIR)
    if types_react is not None or types_react_dom is not None:
        mm = [_major_minor(r) if r else None for r in (types_react, types_react_dom)]
        if None in mm or mm[0] != mm[1]:
            problems.append(
                f"{where}: @types/react is {types_react!r} but @types/react-dom is {types_react_dom!r} "
                "(must be on the same major.minor)"
            )
    return problems


def react_pair_problems(package_json: dict, lockfile: dict) -> list[str]:
    """Every way ``package.json`` + ``package-lock.json`` break the pairing."""
    problems = _declared_problems(package_json, "ui/package.json")
    packages = lockfile.get("packages")
    if not isinstance(packages, dict) or "" not in packages:
        return [*problems, "ui/package-lock.json: no lockfileVersion>=2 `packages` map with a root entry"]
    # The lock's root entry is what `npm ci` checks package.json against; a
    # hand edit to only one of the two files is caught here too.
    problems += _declared_problems(packages[""], "ui/package-lock.json root entry")

    # Every installed copy, not just the hoisted one: a dependency carrying its
    # own nested react-dom gets bundled with the same #527 result.
    resolved: dict[str, set[str]] = {name: set() for name in RUNTIME_PAIR}
    for path, entry in packages.items():
        for name in RUNTIME_PAIR:
            if path == f"node_modules/{name}" or path.endswith(f"/node_modules/{name}"):
                resolved[name].add(str(entry.get("version")))
    for name in RUNTIME_PAIR:
        if not resolved[name]:
            problems.append(f"ui/package-lock.json: {name} is not installed at all")
        elif len(resolved[name]) > 1:
            problems.append(f"ui/package-lock.json: {name} resolves to several versions {sorted(resolved[name])}")
    all_runtime = resolved["react"] | resolved["react-dom"]
    if resolved["react"] and resolved["react-dom"] and len(all_runtime) > 1:
        problems.append(
            f"ui/package-lock.json: react resolves to {sorted(resolved['react'])} but react-dom to "
            f"{sorted(resolved['react-dom'])} — the bundle throws React error #527 at load and renders nothing"
        )

    types_versions = [packages.get(f"node_modules/{name}", {}).get("version") for name in TYPES_PAIR]
    if any(types_versions):
        mm = [_major_minor(v) if v else None for v in types_versions]
        if None in mm or mm[0] != mm[1]:
            problems.append(
                f"ui/package-lock.json: @types/react resolves to {types_versions[0]!r} but @types/react-dom "
                f"to {types_versions[1]!r} (must be on the same major.minor)"
            )
    return problems


def _load(ui_dir: Path) -> tuple[dict, dict]:
    package_json = json.loads((ui_dir / "package.json").read_text(encoding="utf-8"))
    lockfile = json.loads((ui_dir / "package-lock.json").read_text(encoding="utf-8"))
    return package_json, lockfile


def test_ui_ships_react_and_react_dom_at_the_same_version() -> None:
    problems = react_pair_problems(*_load(UI_DIR))
    assert not problems, "\n".join(problems)


def test_dependabot_bumps_the_four_react_packages_in_one_pr() -> None:
    """The upstream half: #1872 was a Dependabot PR that moved react alone.

    Dependabot assigns a dependency to the first group whose patterns match,
    per update type, so each type needs one group naming all four.
    """
    config = yaml.safe_load(DEPENDABOT_YML.read_text(encoding="utf-8"))
    ui = [u for u in config["updates"] if u["package-ecosystem"] == "npm" and u["directory"] == "/ui"]
    assert len(ui) == 1, ui
    groups = ui[0].get("groups", {})
    wanted = {*RUNTIME_PAIR, *TYPES_PAIR}
    for update_type in ("version-updates", "security-updates"):
        covering = [
            name
            for name, group in groups.items()
            if group.get("applies-to", "version-updates") == update_type and wanted <= set(group.get("patterns", []))
        ]
        assert covering, f"no {update_type} group in {DEPENDABOT_YML.name} names all of {sorted(wanted)}: {groups}"


def _tree(
    react: str,
    react_dom: str,
    types_react: str = "19.3.0",
    types_react_dom: str = "19.3.0",
    *,
    react_range: str | None = None,
    react_dom_range: str | None = None,
    extra_packages: dict | None = None,
) -> tuple[dict, dict]:
    manifest = {
        "dependencies": {"react": react_range or f"^{react}", "react-dom": react_dom_range or f"^{react_dom}"},
        "devDependencies": {"@types/react": f"^{types_react}", "@types/react-dom": f"^{types_react_dom}"},
    }
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": json.loads(json.dumps(manifest)),
            "node_modules/react": {"version": react},
            "node_modules/react-dom": {"version": react_dom, "peerDependencies": {"react": f"^{react_dom}"}},
            "node_modules/@types/react": {"version": types_react, "dev": True},
            "node_modules/@types/react-dom": {"version": types_react_dom, "dev": True},
            **(extra_packages or {}),
        },
    }
    return manifest, lock


class TestTheCheckRejects:
    """The guard, fed the inputs it exists to reject."""

    def test_the_incident_tree_from_pr_1872(self) -> None:
        """main between #1872 (2026-09-25) and #1905 (2026-10-01)."""
        problems = react_pair_problems(*_tree("19.3.0", "19.2.8", "19.3.0", "19.2.5"))
        joined = "\n".join(problems)
        assert any("React error #527" in p and "ui/package-lock.json" in p for p in problems), joined
        assert any(p.startswith("ui/package.json: react is") for p in problems), joined
        assert any("@types/react resolves to '19.3.0'" in p for p in problems), joined

    def test_a_patch_level_difference_alone(self) -> None:
        """#527 is an exact comparison, so 19.3.0 vs 19.3.1 is just as fatal."""
        problems = react_pair_problems(*_tree("19.3.1", "19.3.0", react_range="^19.3.0", react_dom_range="^19.3.0"))
        assert problems == [
            "ui/package-lock.json: react resolves to ['19.3.1'] but react-dom to ['19.3.0'] — "
            "the bundle throws React error #527 at load and renders nothing"
        ]

    def test_a_nested_react_dom_copy(self) -> None:
        nested = {"node_modules/some-widget/node_modules/react-dom": {"version": "19.2.8"}}
        problems = react_pair_problems(*_tree("19.3.0", "19.3.0", extra_packages=nested))
        assert any("react-dom resolves to several versions ['19.2.8', '19.3.0']" in p for p in problems), problems

    def test_types_on_different_minors(self) -> None:
        problems = react_pair_problems(*_tree("19.3.0", "19.3.0", "19.3.0", "19.2.5"))
        assert len(problems) == 3, problems  # package.json, lock root entry, lock resolved
        assert all("@types/react" in p for p in problems), problems

    def test_a_missing_react_dom(self) -> None:
        manifest, lock = _tree("19.3.0", "19.3.0")
        del lock["packages"]["node_modules/react-dom"]
        assert "ui/package-lock.json: react-dom is not installed at all" in react_pair_problems(manifest, lock)

    def test_a_lockfile_without_a_packages_map(self) -> None:
        manifest, _ = _tree("19.3.0", "19.3.0")
        assert react_pair_problems(manifest, {"lockfileVersion": 1, "dependencies": {}})

    @pytest.mark.parametrize("types_dom", ["19.3.0", "19.3.7"])
    def test_and_accepts_a_matched_tree(self, types_dom: str) -> None:
        """Same major.minor is enough for the type packages; patch may differ."""
        assert react_pair_problems(*_tree("19.3.0", "19.3.0", "19.3.2", types_dom)) == []
