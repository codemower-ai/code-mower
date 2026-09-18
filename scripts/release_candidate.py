"""Build once, inspect, and verify an exact-source v1.5.0 artifact pair.

This script never tags, publishes, contacts Slack or invokes a provider. The
candidate workflow supplies the merged PR identity; local builds are rehearsals.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zipfile

VERSION = "1.5.0"
SCHEMA = "code_mower.release_candidate.v1"
NAMES = (f"code_mower-{VERSION}-py3-none-any.whl", f"code_mower-{VERSION}.tar.gz")
MODULES = (
    "context_graph_lifecycle.py", "context_graph_query.py", "context_graph_command.py",
    "context_graph_connection.py",
    "slack_setup.py", "slack_readiness.py", "supervisor_contract_v2.py",
    "templates/slack/hosted-app-manifest.json",
)
DOCS = ("v150-release-notes.md", "v150-qualification.md", "v150-release-runbook.md",
        "slack-setup.md", "graphify-setup.md")


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def inspect(dist: Path):
    """Inventory both containers without extraction or product execution."""
    wheel, sdist = (dist / name for name in NAMES)
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        metadata = BytesParser().parsebytes(archive.read(f"code_mower-{VERSION}.dist-info/METADATA"))
        require(metadata["Name"] == "code-mower" and metadata["Version"] == VERSION,
                "wheel identity mismatch")
        deps = metadata.get_all("Requires-Dist", [])
        # Only a solely extra-gated dependency may be excluded from the base
        # inventory. A mixed marker (Python/platform OR extra) can install by
        # default and must not accidentally hide an added Slack dependency.
        base_deps = [dep for dep in deps if not re.fullmatch(
            r"extra\s*==\s*['\"](?:coworker|test)['\"]", dep.partition(";")[2].strip())]
        require(sorted(dep.lower().split(">=")[0] for dep in base_deps) == ["packaging", "pyyaml"],
                "unexpected base dependencies")
        for module in MODULES:
            require("code_mower/" + module in wheel_names, "missing required wheel module")
        for doc in DOCS:
            require(f"code_mower-{VERSION}.data/data/share/code-mower/docs/{doc}" in wheel_names,
                    "missing required wheel documentation")
    with tarfile.open(sdist) as archive:
        members = archive.getmembers()
        sdist_names = [member.name for member in members]
        require(all(member.isfile() or member.isdir() for member in members),
                "sdist contains links or special files")
        for module in MODULES:
            require(f"code_mower-{VERSION}/src/code_mower/{module}" in sdist_names,
                    "missing required sdist module")
        for doc in DOCS:
            require(f"code_mower-{VERSION}/docs/{doc}" in sdist_names,
                    "missing required sdist documentation")
    for names in (wheel_names, sdist_names):
        require(len(names) == len(set(names)), "duplicate archive paths")
        require(all(not name.startswith("/") and not
                    ({"..", ".git", ".code-mower", ".graph", ".graphify", "graphify-out",
                      "__pycache__", ".env"} & set(name.split("/")))
                    and not name.endswith((".pyc", ".pyo")) for name in names),
                "unsafe or private archive inventory")
    return {"wheel_files": sorted(wheel_names), "sdist_files": sorted(sdist_names),
            "default_dependencies": sorted(base_deps), "required_modules": list(MODULES),
            "required_docs": list(DOCS)}


def verify(dist: Path, sha: str, *, candidate=False):
    require(sorted(p.name for p in dist.iterdir() if p.name.endswith((".whl", ".tar.gz"))) == sorted(NAMES),
            "unexpected distribution files")
    require(all((dist / name).is_file() and not (dist / name).is_symlink() for name in NAMES),
            "artifact files must be regular files")
    manifest = json.loads((dist / "candidate.json").read_text())
    require(manifest.get("schema") == SCHEMA and manifest.get("version") == VERSION,
            "invalid candidate identity")
    require(manifest.get("source_sha") == sha and re.fullmatch(r"[0-9a-f]{40}", sha),
            "candidate source SHA mismatch")
    if candidate:
        require(manifest.get("kind") == "candidate" and
                type(manifest.get("release_pr")) is int and manifest["release_pr"] > 0,
                "a pre-merge rehearsal is not a release candidate")
    require(set(manifest.get("artifacts", {})) == set(NAMES), "invalid artifact pair")
    for name in NAMES:
        require(manifest["artifacts"][name] == digest(dist / name), "artifact digest mismatch")
    require(manifest.get("inventory") == inspect(dist), "artifact inventory mismatch")
    return manifest


def build(source: Path, dist: Path, sha: str, release_pr: int | None):
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "a full source SHA is required")
    require(run("git", "rev-parse", "HEAD", cwd=source) == sha, "checkout is not the requested SHA")
    require(not run("git", "status", "--porcelain", "--untracked-files=all", cwd=source),
            "candidate source must be clean")
    require(not dist.exists(), "output already exists; never overwrite an artifact pair")
    require(not dist.is_relative_to(source), "build output must be outside the source checkout")
    run(sys.executable, str(source / "src/code_mower/release_identity.py"),
        "--repo", str(source), "--tag", "v" + VERSION)
    dist.mkdir(parents=True)
    # Stable archive timestamps; dependency downloads are package build tools only.
    env = dict(os.environ, SOURCE_DATE_EPOCH=run("git", "show", "-s", "--format=%ct", sha, cwd=source))
    subprocess.run([sys.executable, "-m", "build", "--outdir", str(dist), str(source)],
                   env=env, check=True)
    require(sorted(p.name for p in dist.iterdir()) == sorted(NAMES), "unexpected build outputs")
    subprocess.run([sys.executable, "-m", "twine", "check", *(str(dist / n) for n in NAMES)], check=True)
    with zipfile.ZipFile(dist / NAMES[0]) as wheel, tarfile.open(dist / NAMES[1]) as sdist:
        for module in MODULES:
            original = (source / "src/code_mower" / module).read_bytes()
            require(wheel.read("code_mower/" + module) == original, "wheel/source inclusion mismatch")
            require(sdist.extractfile(f"code_mower-{VERSION}/src/code_mower/{module}").read() == original,
                    "sdist/source inclusion mismatch")
        for doc in DOCS:
            original = (source / "docs" / doc).read_bytes()
            require(wheel.read(f"code_mower-{VERSION}.data/data/share/code-mower/docs/{doc}") == original,
                    "wheel/source documentation mismatch")
            require(sdist.extractfile(f"code_mower-{VERSION}/docs/{doc}").read() == original,
                    "sdist/source documentation mismatch")
    manifest = {"schema": SCHEMA, "version": VERSION, "source_sha": sha,
                "kind": "candidate" if release_pr else "rehearsal", "release_pr": release_pr,
                "artifacts": {n: digest(dist / n) for n in NAMES}, "inventory": inspect(dist)}
    (dist / "candidate.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    verify(dist, sha, candidate=bool(release_pr))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify"))
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--release-pr", type=int)
    parser.add_argument("--require-candidate", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "build":
            if args.release_pr is not None:
                # Do not let a local build assert that an unmerged PR is qualified.
                require(args.release_pr > 0, "invalid release PR")
                pr = json.loads(run("gh", "pr", "view", str(args.release_pr), "--repo",
                                    "codemower-ai/code-mower", "--json", "state,mergeCommit"))
                require(pr["state"] == "MERGED" and pr["mergeCommit"]["oid"] == args.source_sha,
                        "candidate must bind the release PR's actual merge SHA")
            result = build(args.source.resolve(), args.dist.resolve(), args.source_sha, args.release_pr)
        else:
            result = verify(args.dist.resolve(), args.source_sha, candidate=args.require_candidate)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Candidate refused: {exc}\n")
    print(json.dumps({key: result[key] for key in
                      ("schema", "version", "source_sha", "kind", "release_pr", "artifacts")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
