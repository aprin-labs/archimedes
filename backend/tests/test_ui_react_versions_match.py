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


# ── Dependabot: the four must land in ONE group, for every kind of bump ──
#
# A model of Dependabot's documented assignment rules (docs.github.com, "Optimizing
# PR creation for Dependabot version updates" and the dependabot.yml reference),
# limited to the keys that can split the four:
#   * per update type (`applies-to`, default version-updates), a dependency joins
#     the FIRST group, in file order, whose `patterns` (default: everything) match
#     its name, whose `exclude-patterns` do not, whose `dependency-type`
#     (production / development) is its own, and whose `update-types`
#     (major / minor / patch) include this bump; no group = a PR of its own;
#   * an `ignore` rule naming some of the four stops their bumps, not the others';
#   * an `allow` list, when present, is the only set Dependabot updates at all.
# Patterns use `*` as the only wildcard.

SEMVER_LEVELS = ("major", "minor", "patch")
UPDATE_TYPES = ("version-updates", "security-updates")


def _dependabot_glob(pattern: str, name: str) -> bool:
    regex = ".*".join(re.escape(part) for part in str(pattern).split("*"))
    return re.fullmatch(regex, name, flags=re.IGNORECASE) is not None


def dependabot_group_for(update: dict, package: str, dep_type: str, applies_to: str, level: str) -> str | None:
    """The group Dependabot puts this bump of ``package`` in, or ``None`` (its own PR)."""
    for name, group in (update.get("groups") or {}).items():
        group = group or {}
        if group.get("applies-to", "version-updates") != applies_to:
            continue
        if "dependency-type" in group and group["dependency-type"] != dep_type:
            continue
        if "update-types" in group and level not in group["update-types"]:
            continue
        if not any(_dependabot_glob(p, package) for p in group.get("patterns", ["*"])):
            continue
        if any(_dependabot_glob(p, package) for p in group.get("exclude-patterns", [])):
            continue
        return name
    return None


def _allowed(update: dict, package: str, dep_type: str) -> bool:
    rules = update.get("allow")
    if not rules:
        return True
    for rule in rules:
        if "dependency-name" in rule and not _dependabot_glob(rule["dependency-name"], package):
            continue
        if rule.get("dependency-type", "all") in ("all", "direct", dep_type):
            return True
    return False


def react_grouping_problems(config: dict, dep_types: dict[str, str]) -> list[str]:
    """Every way ``dependabot.yml`` can open a PR that moves some of the four but not all."""
    ui = [
        u
        for u in config.get("updates", [])
        if u.get("package-ecosystem") == "npm" and "/ui" in [u.get("directory"), *(u.get("directories") or [])]
    ]
    if len(ui) != 1:
        return [f"expected exactly one npm update entry for /ui, found {len(ui)}"]
    update = ui[0]
    problems: list[str] = []
    for package, dep_type in dep_types.items():
        if not _allowed(update, package, dep_type):
            problems.append(f"`allow` leaves {package} out: it is never bumped, so the others move without it")
    for rule in update.get("ignore") or []:
        hit = sorted(p for p in dep_types if _dependabot_glob(rule.get("dependency-name", ""), p))
        if hit and (len(hit) != len(dep_types) or "versions" in rule):
            problems.append(f"ignore rule {rule} holds back {hit} while the rest of the four still move")
    for applies_to in UPDATE_TYPES:
        for level in SEMVER_LEVELS:
            assigned = {p: dependabot_group_for(update, p, t, applies_to, level) for p, t in dep_types.items()}
            if None in assigned.values() or len(set(assigned.values())) != 1:
                problems.append(f"{applies_to}, {level} bump: the four land in {assigned} (None = a PR of its own)")
    return problems


def _ui_dep_types() -> dict[str, str]:
    manifest = json.loads((UI_DIR / "package.json").read_text(encoding="utf-8"))
    types = {}
    for name in (*RUNTIME_PAIR, *TYPES_PAIR):
        types[name] = "production" if name in manifest.get("dependencies", {}) else "development"
    return types


def test_dependabot_bumps_the_four_react_packages_in_one_pr() -> None:
    """The upstream half: #1872 was a Dependabot PR that moved react alone."""
    config = yaml.safe_load(DEPENDABOT_YML.read_text(encoding="utf-8"))
    dep_types = _ui_dep_types()
    assert dep_types == {
        "react": "production",
        "react-dom": "production",
        "@types/react": "development",
        "@types/react-dom": "development",
    }, dep_types
    problems = react_grouping_problems(config, dep_types)
    assert not problems, "\n".join(problems)


def _dependabot(groups: dict, **update_keys: object) -> dict:
    return {
        "version": 2,
        "updates": [{"package-ecosystem": "npm", "directory": "/ui", "groups": groups, **update_keys}],
    }


_FOUR = ["react", "react-dom", "@types/react", "@types/react-dom"]
_GOOD_GROUPS = {
    "react": {"applies-to": "version-updates", "patterns": _FOUR},
    "react-security": {"applies-to": "security-updates", "patterns": _FOUR},
}
_DEP_TYPES = {
    "react": "production",
    "react-dom": "production",
    "@types/react": "development",
    "@types/react-dom": "development",
}


class TestTheGroupingCheckRejects:
    """react_grouping_problems, fed configs that split the four."""

    @pytest.mark.parametrize(
        ("label", "config"),
        [
            # The #1872 shape: react-dom is not in the group, so it moves alone.
            ("react-dom dropped", _dependabot({**_GOOD_GROUPS, "react": {"patterns": _FOUR[:1] + _FOUR[2:]}})),
            # First match wins: an earlier group takes react + react-dom, so the
            # types land in `react` and the runtime pair in another PR. A superset
            # check of the `react` group's patterns passes this one.
            ("earlier group", _dependabot({"runtime": {"patterns": ["react", "react-dom"]}, **_GOOD_GROUPS})),
            ("excluded", _dependabot({**_GOOD_GROUPS, "react": {"patterns": _FOUR, "exclude-patterns": ["@types/*"]}})),
            ("prod only", _dependabot({**_GOOD_GROUPS, "react": {"patterns": _FOUR, "dependency-type": "production"}})),
            ("no majors", _dependabot({**_GOOD_GROUPS, "react": {"patterns": _FOUR, "update-types": ["minor"]}})),
            ("no security group", _dependabot({"react": _GOOD_GROUPS["react"]})),
            (
                "ignored",
                _dependabot(
                    _GOOD_GROUPS,
                    ignore=[{"dependency-name": "react-dom", "update-types": ["version-update:semver-minor"]}],
                ),
            ),
            ("version-pinned", _dependabot(_GOOD_GROUPS, ignore=[{"dependency-name": "*", "versions": [">=20"]}])),
            ("allow-listed", _dependabot(_GOOD_GROUPS, allow=[{"dependency-name": "react"}])),
            ("two /ui entries", {"updates": [_dependabot(_GOOD_GROUPS)["updates"][0]] * 2}),
        ],
    )
    def test_a_split_is_named(self, label: str, config: dict) -> None:
        assert react_grouping_problems(config, _DEP_TYPES), label

    @pytest.mark.parametrize(
        ("label", "config"),
        [
            ("as committed", _dependabot(_GOOD_GROUPS)),
            (
                "wildcards",
                _dependabot({"r": {"patterns": ["react*", "@types/react*"]}, "s": {"applies-to": "security-updates"}}),
            ),
            ("one catch-all group first", _dependabot({"all": {"patterns": ["*"]}, **_GOOD_GROUPS})),
            (
                "an ignore rule that holds all four alike",
                _dependabot(
                    _GOOD_GROUPS,
                    ignore=[{"dependency-name": "*react*", "update-types": ["version-update:semver-major"]}],
                ),
            ),
            ("allow all direct deps", _dependabot(_GOOD_GROUPS, allow=[{"dependency-type": "direct"}])),
        ],
    )
    def test_and_accepts_configs_that_keep_them_together(self, label: str, config: dict) -> None:
        assert react_grouping_problems(config, _DEP_TYPES) == [], label


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
