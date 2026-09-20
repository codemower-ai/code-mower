"""Build once, inspect, and verify an exact-source v1.5.1 artifact pair.

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

VERSION = "1.5.1"
SCHEMA = "code_mower.release_candidate.v1"
NAMES = (f"code_mower-{VERSION}-py3-none-any.whl", f"code_mower-{VERSION}.tar.gz")
MODULES = (
    "context_graph_lifecycle.py", "context_graph_query.py", "context_graph_command.py",
    "context_graph_connection.py",
    "slack_setup.py", "slack_readiness.py", "supervisor_contract_v2.py",
    "templates/slack/hosted-app-manifest.json",
)
DOCS = ("v151-release-notes.md", "v151-qualification.md", "v151-release-runbook.md",
        "slack-setup.md", "graphify-setup.md")
REHEARSAL_SCHEMA = "code_mower.v151_rehearsal.v1"
CANARY_EQUIVALENCE_SCHEMA = "code_mower.canary_candidate_equivalence.v1"
GRAPHIFY_CHECKS = (
    "graphify_doc_ref_excluded_reader_available",
    "graphify_ambiguity_only_partial_usable_complete_generation",
    "graphify_wrong_distribution_reader_incompatible_no_leakage",
)
REHEARSAL_CHECKS = (
    "fresh_default_slack_free_no_network_or_service",
    "explicit_slack_setup_exclusive_private_manifest",
    "all_green_offline_snapshot_cannot_claim_live_readiness",
    "offline_disabled_snapshot_and_local_manifest_removal",
    *GRAPHIFY_CHECKS,
    "upgrade_1_4_2_to_exact_wheel_preserves_synthetic_state",
    "disposable_rollback_to_digest_verified_1_4_2_preserves_state",
    "uninstall_preserves_synthetic_state",
)

# A one-release exception for carrying already accepted paid canary outcomes
# across the reviewed release-closeout changes. Every wheel member outside
# these audit/release/documentation surfaces must remain byte-identical. Keep
# this list narrow: Slack, supervisor, provider, entry-point and dependency
# changes must force new live canaries under a new explicit authorization.
CANARY_EQUIVALENCE_ALLOWED_WHEEL_CHANGES = frozenset({
    "code_mower/audit_publication.py",
    "code_mower/release_readiness.py",
    "code_mower/templates/workflows/local-audit-publication.yml.j2",
    "code_mower/templates/workflows/trailer-comment-labeler.yml.j2",
    f"code_mower-{VERSION}.data/data/share/code-mower/docs/graphify-setup.md",
    f"code_mower-{VERSION}.data/data/share/code-mower/docs/v151-qualification.md",
    f"code_mower-{VERSION}.data/data/share/code-mower/docs/v151-release-notes.md",
    f"code_mower-{VERSION}.data/data/share/code-mower/docs/v151-release-runbook.md",
    f"code_mower-{VERSION}.dist-info/METADATA",
    f"code_mower-{VERSION}.dist-info/RECORD",
})


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


def verify_rehearsal(dist: Path, manifest: dict):
    """Require wheel-bound lifecycle/Graphify evidence from a verified manifest."""
    wheels = [name for name in manifest["artifacts"] if name.endswith(".whl")]
    require(len(wheels) == 1, "candidate identity requires exactly one wheel")
    evidence = json.loads((dist / "rehearsal.json").read_text())
    require(evidence.get("schema") == REHEARSAL_SCHEMA and evidence.get("status") == "pass",
            "invalid rehearsal identity or status")
    require(evidence.get("source_sha") == manifest["source_sha"], "rehearsal source SHA mismatch")
    require(evidence.get("artifact_sha256") == manifest["artifacts"][wheels[0]],
            "rehearsal wheel digest mismatch")
    checks = evidence.get("checks")
    require(isinstance(checks, list) and all(isinstance(check, str) for check in checks)
            and set(REHEARSAL_CHECKS) <= set(checks), "missing required rehearsal checks")
    return evidence


def _wheel_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _metadata_headers(raw: bytes) -> list[tuple[str, str]]:
    metadata = BytesParser().parsebytes(raw)
    return sorted((name.lower(), value.strip()) for name, value in metadata.items())


def compare_canary_surface(prior_dist: Path, final_dist: Path, prior_sha: str,
                           final_sha: str, source: Path):
    """Attest that a final candidate did not change the paid-canary surface.

    This does not qualify either candidate or replace final-candidate private
    acceptance. It only establishes that already accepted live canaries may be
    carried across the bounded v1.5.0 release-closeout delta documented in the
    qualification contract.
    """
    require(prior_sha != final_sha, "candidate comparison requires distinct source SHAs")
    prior = verify(prior_dist, prior_sha, candidate=True)
    final = verify(final_dist, final_sha, candidate=True)
    run("git", "merge-base", "--is-ancestor", prior_sha, final_sha, cwd=source)

    prior_wheel = prior_dist / NAMES[0]
    final_wheel = final_dist / NAMES[0]
    before = _wheel_members(prior_wheel)
    after = _wheel_members(final_wheel)
    require(set(before) == set(after), "wheel member inventory changed")
    changed = sorted(name for name in before if before[name] != after[name])
    disallowed = sorted(set(changed) - CANARY_EQUIVALENCE_ALLOWED_WHEEL_CHANGES)
    require(not disallowed, "paid-canary surface changed: " + ", ".join(disallowed))
    require(changed, "candidate comparison found no wheel member changes")

    metadata_name = f"code_mower-{VERSION}.dist-info/METADATA"
    if metadata_name in changed:
        require(_metadata_headers(before[metadata_name]) == _metadata_headers(after[metadata_name]),
                "package metadata headers changed")

    required_canary_members = sorted("code_mower/" + module for module in MODULES)
    require(all(before[name] == after[name] for name in required_canary_members),
            "required Slack, Graphify or supervisor module changed")
    unchanged = len(before) - len(changed)
    changed_digests = {
        name: {
            "prior": hashlib.sha256(before[name]).hexdigest(),
            "final": hashlib.sha256(after[name]).hexdigest(),
        }
        for name in changed
    }
    return {
        "schema": CANARY_EQUIVALENCE_SCHEMA,
        "status": "pass",
        "prior_source_sha": prior_sha,
        "final_source_sha": final_sha,
        "prior_release_pr": prior["release_pr"],
        "final_release_pr": final["release_pr"],
        "prior_wheel_sha256": prior["artifacts"][NAMES[0]],
        "final_wheel_sha256": final["artifacts"][NAMES[0]],
        "changed_wheel_members": changed,
        "changed_wheel_member_sha256": changed_digests,
        "unchanged_wheel_member_count": unchanged,
        "metadata_headers_unchanged": True,
        "required_canary_members_unchanged": True,
    }


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
    parser.add_argument("action", choices=("build", "verify", "compare-canary-surface"))
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--prior-dist", type=Path)
    parser.add_argument("--prior-source-sha")
    parser.add_argument("--report", type=Path)
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
        elif args.action == "verify":
            result = verify(args.dist.resolve(), args.source_sha, candidate=args.require_candidate)
        else:
            require(args.prior_dist is not None and args.prior_source_sha is not None,
                    "candidate comparison requires --prior-dist and --prior-source-sha")
            result = compare_canary_surface(
                args.prior_dist.resolve(), args.dist.resolve(), args.prior_source_sha,
                args.source_sha, args.source.resolve())
            if args.report:
                require(not args.report.exists(), "comparison report already exists")
                args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Candidate refused: {exc}\n")
    if args.action == "compare-canary-surface":
        output = result
    else:
        output = {key: result[key] for key in
                  ("schema", "version", "source_sha", "kind", "release_pr", "artifacts")}
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
