"""`environment.yml` and `backend/requirements.txt` must agree on shared floors.

CLAUDE.md § Linting, formatting, dependencies: "Keep `environment.yml` (local dev)
and `backend/requirements.txt` (Docker / CI) aligned. Drift is the most common
source of 'works on my machine' + 'breaks in CI'."

Nothing enforced that until #1493, and it had drifted on 18 of 34 shared packages —
every one with the local floor *below* the deployed one, so a conda env could
resolve pandas 2.x while production ran 3.x.

Issue #1522 removed most of that duplication rather than policing it: the
shared floors moved to `backend/requirements-base.txt`, which both files now pull
in with `-r`. The parsers below **resolve those includes**, so the guard still
compares the same 34 shared floors it compared before the split. Two things can
still go wrong, and both are covered here:

1. The include stops resolving — a renamed or mistyped path, a file deleted.
   Without resolution the parsed set collapses to a handful of entries and the
   alignment assertions would pass vacuously, which is exactly the failure mode
   CLAUDE.md rule 4 warns about. `test_both_files_parse_into_a_meaningful_shared_set`
   and `test_the_shared_base_is_reached_through_the_include` fail loudly instead.
2. A floor is written down twice with different values — `torch` and
   `sentence-transformers` legitimately are (the CPU-wheel index cannot reach
   `environment.yml`), and anyone re-adding a third override to either file
   reopens the original drift. `test_shared_dependency_floors_are_identical`
   catches the value mismatch; `test_only_torch_and_sentence_transformers_are_still_written_down_twice`
   catches the new override itself, before it has a chance to drift.

Deliberately stdlib-only: no PyYAML, no `packaging`. Neither is a declared
dependency of the backend, and a guard that can only run when an undeclared
transitive import happens to be present is not a guard.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT_YML = REPO_ROOT / "environment.yml"
REQUIREMENTS_TXT = REPO_ROOT / "backend" / "requirements.txt"
REQUIREMENTS_BASE_TXT = REPO_ROOT / "backend" / "requirements-base.txt"

# The two packages that must stay written down in both files: `environment.yml`
# cannot include the file that pins them, because that file also carries
# `--extra-index-url https://download.pytorch.org/whl/cpu` and the CPU wheel must
# not reach a dev machine (it costs MPS on Apple Silicon).
TORCH_OVERRIDES = frozenset({"torch", "sentence-transformers"})

# A dependency line: optional quotes, a name, optional extras, then the specifier.
_DEP = re.compile(r'^"?(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?(?P<spec>.*)$')
_FLOOR = re.compile(r">=\s*(?P<version>[0-9][0-9A-Za-z.*+!-]*)")
# `-r other.txt` / `--requirement other.txt`, as pip accepts it.
_INCLUDE = re.compile(r"^(?:-r|--requirement)[=\s]+(?P<path>\S+)$")


def _canonical(name: str) -> str:
    """PEP 503 normalisation, so `python_dotenv` and `python-dotenv` are one key."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def _parse_dep(line: str) -> tuple[str, str] | None:
    """Return (canonical name, floor) for a dependency line, or None if it isn't one."""
    body = _strip_comment(line)
    # `@ git+...` pins, index directives and bare option lines carry no comparable floor.
    if not body or body.startswith("-") or "@" in body:
        return None
    match = _DEP.match(body)
    if match is None:
        return None
    floor = _FLOOR.search(match.group("spec") or "")
    if floor is None:
        return None
    return _canonical(match.group("name")), floor.group("version")


def _parse_include(line: str, relative_to: Path) -> Path | None:
    """Return the file an `-r` line points at, resolved the way pip resolves it.

    pip resolves a nested requirements file relative to the directory of the file
    that includes it — not the process CWD. `environment.yml`'s pip block behaves
    the same way because conda runs pip with `cwd` set to the environment file's
    directory (`conda.env.pip_util.get_pip_workdir`).
    """
    match = _INCLUDE.match(_strip_comment(line))
    if match is None:
        return None
    return (relative_to / match.group("path")).resolve()


def _floors_from_requirements(path: Path, _seen: set[Path] | None = None) -> dict[str, str]:
    """Floors declared by a requirements file, following `-r` includes.

    Later declarations win, matching pip: the last specifier pip reads for a
    package is the one that ends up constraining the resolve. That ordering is
    what makes a re-added override in `environment.yml` visible as a mismatch
    rather than being masked by the included base.
    """
    seen = set() if _seen is None else _seen
    resolved = path.resolve()
    if not resolved.exists():
        raise AssertionError(
            f"a `-r` line points at {resolved}, which does not exist. pip would fail the Docker "
            "build and the CI install with 'Could not open requirements file'."
        )
    if resolved in seen:  # pip errors on include cycles; just don't loop.
        return {}
    seen.add(resolved)

    out: dict[str, str] = {}
    for line in resolved.read_text().splitlines():
        include = _parse_include(line, resolved.parent)
        if include is not None:
            out.update(_floors_from_requirements(include, seen))
            continue
        parsed = _parse_dep(line)
        if parsed is not None:
            out[parsed[0]] = parsed[1]
    return out


def _own_floors(path: Path) -> dict[str, str]:
    """Floors written down in this file itself, ignoring anything it includes."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        parsed = _parse_dep(line)
        if parsed is not None:
            out[parsed[0]] = parsed[1]
    return out


def _environment_pip_entries() -> list[str]:
    """The raw entries of the `pip:` block of environment.yml, without a YAML parser.

    The block is the run of `      - dep` lines under `  - pip:`; it ends at the
    first line that is neither blank, nor a comment, nor indented past `pip:`.
    """
    entries: list[str] = []
    in_pip = False
    for line in ENVIRONMENT_YML.read_text().splitlines():
        if re.match(r"^\s*-\s*pip:\s*$", line):
            in_pip = True
            continue
        if not in_pip:
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent < 6:
            break
        entries.append(line.lstrip().removeprefix("- "))
    return entries


def _environment_floors() -> dict[str, str]:
    """Effective pip floors for the conda env, following `-r` includes."""
    out: dict[str, str] = {}
    for entry in _environment_pip_entries():
        include = _parse_include(entry, ENVIRONMENT_YML.parent)
        if include is not None:
            out.update(_floors_from_requirements(include))
            continue
        parsed = _parse_dep(entry)
        if parsed is not None:
            out[parsed[0]] = parsed[1]
    return out


def _requirements_floors() -> dict[str, str]:
    return _floors_from_requirements(REQUIREMENTS_TXT)


def _shared() -> list[str]:
    return sorted(set(_environment_floors()) & set(_requirements_floors()))


def test_both_files_parse_into_a_meaningful_shared_set() -> None:
    """Fail loudly if the parser stops matching, rather than passing vacuously.

    Without this, a regex that silently matched nothing — or an `-r` include that
    stopped resolving after the #1522 split — would make the alignment assertion
    below trivially true. That is the exact failure mode CLAUDE.md calls out
    ("a guard must be shown to reject something").
    """
    env, req = _environment_floors(), _requirements_floors()
    assert len(env) >= 30, f"environment.yml pip block parsed to only {len(env)} deps"
    assert len(req) >= 30, f"requirements.txt parsed to only {len(req)} deps"
    assert len(_shared()) >= 30, f"only {len(_shared())} shared deps found; parser likely broken"


def test_the_shared_base_is_reached_through_the_include() -> None:
    """Both files must actually pull in `requirements-base.txt`, and reach its contents.

    The floors themselves are asserted elsewhere; this pins the *mechanism*. If
    `backend/requirements.txt` stopped including the base, the Docker image would
    ship missing every package the base file owns, and every other test here
    would still pass — `torch` and `sentence-transformers` would be the whole
    shared set, and they do agree.
    """
    assert REQUIREMENTS_BASE_TXT.exists(), f"{REQUIREMENTS_BASE_TXT} is missing; both files include it"

    base = _own_floors(REQUIREMENTS_BASE_TXT)
    assert len(base) >= 30, f"requirements-base.txt parsed to only {len(base)} deps"

    for label, effective in (
        ("backend/requirements.txt", _requirements_floors()),
        ("environment.yml", _environment_floors()),
    ):
        missing = sorted(name for name in base if name not in effective)
        assert not missing, (
            f"{label} does not resolve backend/requirements-base.txt — {len(missing)} of its "
            f"{len(base)} packages are unreachable (e.g. {', '.join(missing[:5])}). "
            "Check the `-r` path."
        )
        differing = {name: (effective[name], floor) for name, floor in base.items() if effective[name] != floor}
        assert not differing, f"{label} resolves the base file but overrides its floors for: " + ", ".join(
            f"{n} ({got} vs base {want})" for n, (got, want) in sorted(differing.items())
        )


def test_the_shared_base_carries_no_cpu_wheel_index_or_torch() -> None:
    """The anti-goal of #1522, kept as a permanent check.

    `environment.yml` includes `requirements-base.txt`. Putting the pytorch CPU
    index — or the packages that need it — in that file puts the ~250 MB CPU-only
    build on every dev machine and silently removes MPS acceleration on Apple
    Silicon. That is the defect the split exists to prevent, so grep for it here
    rather than trusting the next editor to remember.
    """
    # Comments are checked separately from directives on purpose: the file's own
    # header explains *why* the index URL is absent, and that prose must not be
    # what makes this test pass. Only executable (non-comment) lines count.
    directives = [
        body
        for body in (_strip_comment(line) for line in REQUIREMENTS_BASE_TXT.read_text().splitlines())
        if "index-url" in body
    ]
    assert not directives, (
        "backend/requirements-base.txt must not carry an index directive — environment.yml "
        f"includes this file, and the pytorch CPU index would follow it onto dev machines. Found: {directives}"
    )
    present = sorted(TORCH_OVERRIDES & set(_own_floors(REQUIREMENTS_BASE_TXT)))
    assert not present, (
        f"{', '.join(present)} must stay in backend/requirements.txt (behind the CPU wheel index), "
        "not in the base file that environment.yml includes."
    )


def test_only_torch_and_sentence_transformers_are_still_written_down_twice() -> None:
    """After #1522 these two are the only floors duplicated by hand — keep it that way.

    Every other shared floor has exactly one home, `requirements-base.txt`. A
    package written down a second time is a new drift site even when the two
    values match *today*: Dependabot edits one file, and the copy silently stops
    tracking it. This is the check that fails if `environment.yml` regresses to
    restating the base — the state #1522 removed — and it fails on the
    duplication itself, before any value has had a chance to diverge.
    """
    # environment.yml is not a requirements file; parse only its own pip entries,
    # deliberately NOT following the `-r` include (whose contents are, by
    # definition, not a second copy).
    env_own: set[str] = set()
    for entry in _environment_pip_entries():
        parsed = _parse_dep(entry)
        if parsed is not None:
            env_own.add(parsed[0])

    base = set(_own_floors(REQUIREMENTS_BASE_TXT))
    req_own = set(_own_floors(REQUIREMENTS_TXT))

    # A name in the base file AND spelled out again by one of its includers.
    restated = sorted((env_own | req_own) & base)
    assert not restated, (
        "these packages are pinned in backend/requirements-base.txt and ALSO written down by a "
        "file that includes it: " + ", ".join(restated) + ". Delete the copy — the include already "
        "supplies the floor, and the copy is exactly the hand-mirroring #1522 removed."
    )

    duplicated = env_own & req_own
    unexpected = sorted(duplicated - TORCH_OVERRIDES)
    assert not unexpected, (
        "these packages are written down in BOTH environment.yml and backend/requirements.txt: "
        + ", ".join(unexpected)
        + ". Only torch and sentence-transformers may be (the CPU wheel index cannot reach "
        "environment.yml). Move the rest into backend/requirements-base.txt, which both include."
    )
    assert duplicated >= TORCH_OVERRIDES, (
        "torch / sentence-transformers should still be pinned in both files: "
        f"found {sorted(duplicated & TORCH_OVERRIDES)}"
    )


def test_shared_dependency_floors_are_identical() -> None:
    env, req = _environment_floors(), _requirements_floors()
    mismatched = {name: (env[name], req[name]) for name in _shared() if env[name] != req[name]}
    assert not mismatched, (
        "environment.yml has drifted from backend/requirements.txt. Local dev would "
        "resolve a different version than Docker/CI for:\n"
        + "\n".join(
            f"  {name}: environment.yml >={e}  vs  requirements.txt >={r}"
            for name, (e, r) in sorted(mismatched.items())
        )
        + "\nRaise the environment.yml floor to match; both files change in the same PR."
    )


def test_no_environment_floor_is_below_the_deployed_floor() -> None:
    """The asymmetric half of the rule, kept separate because it is the harmful one.

    An env floor *above* production is merely strict. An env floor *below* it means
    local dev can run code paths production never executes, which is how a pandas
    2-vs-3 split goes unnoticed.
    """
    env, req = _environment_floors(), _requirements_floors()

    def as_tuple(version: str) -> tuple[int, ...]:
        return tuple(int(part) for part in re.findall(r"\d+", version))

    below = {name: (env[name], req[name]) for name in _shared() if as_tuple(env[name]) < as_tuple(req[name])}
    assert not below, "environment.yml floor is lower than the deployed floor for: " + ", ".join(
        f"{n} ({e} < {r})" for n, (e, r) in sorted(below.items())
    )


# --- The count the prose states must be the count the file has -----------------
#
# Three files tell a reader how many packages `requirements-base.txt` owns:
# its own header, `backend/requirements.txt`'s header, and the comment above
# `environment.yml`'s `-r` include. All three said 32 while the file declared
# 33 (PR #1815 added `pydantic-core`), because nothing connected the sentence
# to the list — the same "nothing enforced that" this module exists for, one
# level up. A stated count is a claim; this makes it a checked one.

# "the 32 non-torch package floors", "The 33 shared floors", "owns the 33 floors".
_STATED_COUNT_RE = re.compile(r"(?i)\bthe\s+(\d+)\s+(?:[A-Za-z][A-Za-z-]*\s+){0,3}floors\b")

_COUNT_CLAIMANTS = ("requirements-base.txt", "requirements.txt", "environment.yml")


def _declared_package_count() -> int:
    """Every package `requirements-base.txt` declares — floors AND the git pin.

    Deliberately not `len(_own_floors(...))`: that parser drops a line carrying no
    `>=` comparison, which is correct for a floor-alignment check and wrong here.
    `circle-titanoboa-sdk[x402,wallets] @ git+...` is a package the file owns and a
    package the prose counts, so it counts here.
    """
    count = 0
    for line in REQUIREMENTS_BASE_TXT.read_text(encoding="utf-8").splitlines():
        body = _strip_comment(line)
        if body and not body.startswith("-"):
            count += 1
    return count


def _prose(path: Path) -> str:
    """A file's text as one line, comment markers removed.

    `environment.yml` wraps its claim across two `#` continuation lines, so a
    per-line reader would miss "owns the 33 / floors that local dev ... need".
    """
    return " ".join(line.lstrip().lstrip("#").strip() for line in path.read_text(encoding="utf-8").splitlines())


def _stated_counts() -> dict[str, list[int]]:
    return {
        path.name: [int(n) for n in _STATED_COUNT_RE.findall(_prose(path))]
        for path in (REQUIREMENTS_BASE_TXT, REQUIREMENTS_TXT, ENVIRONMENT_YML)
    }


def test_the_reader_finds_the_count_each_file_states() -> None:
    """Guard on the guard: a regex that matched nothing would pass the check below
    on any file, forever. Each of the three files carries exactly this claim today."""
    stated = _stated_counts()
    silent = sorted(name for name in _COUNT_CLAIMANTS if not stated.get(name))
    assert not silent, (
        f"no package count parsed out of {silent}. Either the sentence was reworded to "
        "carry no number — in which case delete that file from _COUNT_CLAIMANTS, or delete "
        "this module's count guard entirely — or the reader is broken and the check below "
        "is passing vacuously."
    )


def test_no_file_states_a_package_count_the_base_file_does_not_have() -> None:
    """The claim, checked. Adding a package to `requirements-base.txt` without
    touching the three headers is what made all three say 32 over a 33-package file."""
    actual = _declared_package_count()
    wrong = {name: counts for name, counts in _stated_counts().items() if any(n != actual for n in counts)}
    assert not wrong, (
        f"backend/requirements-base.txt declares {actual} packages, but "
        + "; ".join(f"{name} says {counts}" for name, counts in sorted(wrong.items()))
        + ". Update the sentence with the package, or reword it to carry no count."
    )
