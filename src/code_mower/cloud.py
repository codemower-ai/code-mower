#!/usr/bin/env python3
"""Prepare opt-in Code Mower cloud benchmark bundles."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BUNDLE_SCHEMA = "code_mower.cloudBenchmarkBundle.v1"
UPLOAD_SCHEMA = "code_mower.cloudUpload.v1"
DEFAULT_OUTPUT_DIR = ".code-mower/cloud-benchmark-bundle"
DEFAULT_UPLOAD_ENDPOINT = "https://codemower.com/api/ingest"
DEFAULT_TOKEN_ENV = "CODE_MOWER_CLOUD_TOKEN"
MAX_REPORT_UPLOAD_BYTES = 1_000_000
SAFE_REPORT_KINDS = {
    "authoring-runs",
    "calibration-runs",
    "lane-policy",
    "reviewer-metrics",
    "spend",
    "value-report",
}
EXCLUDED_CONTENT = (
    "source_code",
    "raw_diffs",
    "raw_model_transcripts",
    "raw_stdout_stderr",
    "auth_probe_output",
    "secrets",
)
EXPECTED_BUNDLE_ENTRIES = {
    "README.md",
    "code-mower-cloud-bundle.json",
    "reports",
    ".README.md.tmp",
    ".code-mower-cloud-bundle.json.tmp",
    ".reports.tmp",
}


class CloudBundleError(ValueError):
    """Raised when a cloud bundle request is unsafe or invalid."""


def _safe_kind(value: str) -> str:
    kind = value.strip()
    if kind not in SAFE_REPORT_KINDS:
        allowed = ", ".join(sorted(SAFE_REPORT_KINDS))
        raise CloudBundleError(f"unsupported report kind {value!r}; allowed: {allowed}")
    return kind


def _safe_target_name(path: Path, index: int, *, anonymous: bool = False) -> str:
    suffix = path.suffix.lower()
    if suffix not in {".json", ".jsonl", ".md", ".txt"}:
        raise CloudBundleError(
            f"unsupported report file extension for {path}; expected .json, .jsonl, .md, or .txt"
        )
    if anonymous:
        return f"{index:02d}-report{suffix}"
    stem = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in path.stem)
    stem = stem.strip("-_") or f"report-{index}"
    return f"{index:02d}-{stem}{suffix}"


def _plan_report(
    path: Path,
    output_dir: Path,
    kind: str,
    index: int,
    *,
    anonymous: bool,
) -> dict[str, Any]:
    source = path.expanduser()
    if not source.is_file():
        raise CloudBundleError(f"report does not exist or is not a file: {source}")
    target_name = _safe_target_name(source, index, anonymous=anonymous)
    reports_dir = output_dir / "reports"
    try:
        if source.resolve().is_relative_to(reports_dir.resolve()):
            raise CloudBundleError(
                f"report source must not be inside the bundle reports directory: {source}"
            )
    except OSError as exc:
        raise CloudBundleError(f"unable to resolve report path {source}: {exc}") from exc
    return {
        "kind": kind,
        "source": source,
        "source_basename": "" if anonymous else source.name,
        "target_name": target_name,
        "target": f"reports/{target_name}",
    }


def _stage_reports(planned_reports: list[dict[str, Any]], output_dir: Path) -> tuple[list[dict[str, Any]], Path]:
    stage_dir = output_dir / ".reports.tmp"
    if stage_dir.exists():
        if not stage_dir.is_dir():
            raise CloudBundleError(f"bundle staging path is not a directory: {stage_dir}")
        try:
            shutil.rmtree(stage_dir)
        except OSError as exc:
            raise CloudBundleError(f"unable to clear stale staging directory: {exc}") from exc
    try:
        stage_dir.mkdir(parents=True)
    except OSError as exc:
        raise CloudBundleError(f"unable to create bundle staging directory: {exc}") from exc

    included_reports: list[dict[str, Any]] = []
    try:
        for report in planned_reports:
            source = report["source"]
            target = stage_dir / report["target_name"]
            shutil.copyfile(source, target)
            stat = target.stat()
            included_reports.append(
                {
                    "kind": report["kind"],
                    "source_basename": report["source_basename"],
                    "target": report["target"],
                    "bytes": stat.st_size,
                }
            )
    except OSError as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise CloudBundleError(f"unable to stage bundle reports: {exc}") from exc
    return included_reports, stage_dir


def _swap_reports(stage_dir: Path, reports_dir: Path) -> None:
    try:
        if reports_dir.exists():
            shutil.rmtree(reports_dir)
        stage_dir.replace(reports_dir)
    except OSError as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise CloudBundleError(f"unable to install staged bundle reports: {exc}") from exc


def _existing_bundle_manifest(output_dir: Path) -> bool:
    manifest_path = output_dir / "code-mower-cloud-bundle.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(manifest, dict) and manifest.get("schema") == BUNDLE_SCHEMA


def _unexpected_bundle_entries(output_dir: Path) -> list[str]:
    try:
        return sorted(
            entry.name
            for entry in output_dir.iterdir()
            if entry.name not in EXPECTED_BUNDLE_ENTRIES
        )
    except OSError as exc:
        raise CloudBundleError(f"unable to inspect bundle output directory: {exc}") from exc


def build_cloud_bundle(
    *,
    reports: list[tuple[Path, str]],
    output_dir: Path,
    repo_slug: str = "",
    install_id: str = "",
    team_id: str = "",
    anonymous: bool = False,
) -> dict[str, Any]:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise CloudBundleError(f"bundle output path is not a directory: {output_dir}")
        try:
            has_existing_content = any(output_dir.iterdir())
        except OSError as exc:
            raise CloudBundleError(f"unable to inspect bundle output directory: {exc}") from exc
        if has_existing_content and not _existing_bundle_manifest(output_dir):
            raise CloudBundleError(
                "refusing to write into an existing non-bundle output directory; "
                "choose a fresh --output-dir"
            )
        unexpected_entries = _unexpected_bundle_entries(output_dir)
        if unexpected_entries:
            sample = ", ".join(unexpected_entries[:3])
            if len(unexpected_entries) > 3:
                sample += f", ... ({len(unexpected_entries)} total)"
            raise CloudBundleError(
                "refusing to reuse bundle directory with unexpected entries: "
                f"{sample}"
            )
    else:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CloudBundleError(
                f"unable to create bundle output directory {output_dir}: {exc}"
            ) from exc
    planned_reports = [
        _plan_report(path, output_dir, _safe_kind(kind), index, anonymous=anonymous)
        for index, (path, kind) in enumerate(reports, start=1)
    ]
    included_reports, stage_dir = _stage_reports(planned_reports, output_dir)
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "privacy_mode": "anonymous" if anonymous else "metadata_and_reports",
        "upload_ready": False,
        "upload_status": "local_export_only",
        "repo_slug": "" if anonymous else repo_slug,
        "team_id": "" if anonymous else team_id,
        "install_id": "" if anonymous else install_id,
        "included_reports": included_reports,
        "excluded_content": list(EXCLUDED_CONTENT),
        "notes": [
            "This bundle is local-only; upload support must present a dry-run before network transfer.",
            "Do not include source code, raw diffs, raw transcripts, raw stdout/stderr, auth output, or secrets.",
            "Reports are copied exactly as supplied; review them before sharing outside your machine.",
        ],
    }
    manifest_path = output_dir / "code-mower-cloud-bundle.json"
    readme = output_dir / "README.md"
    manifest_tmp = output_dir / ".code-mower-cloud-bundle.json.tmp"
    readme_tmp = output_dir / ".README.md.tmp"
    try:
        manifest_tmp.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise CloudBundleError(f"unable to write bundle manifest {manifest_tmp}: {exc}") from exc
    try:
        readme_tmp.write_text(render_bundle_readme(manifest), encoding="utf-8")
    except OSError as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        manifest_tmp.unlink(missing_ok=True)
        raise CloudBundleError(f"unable to write bundle README {readme_tmp}: {exc}") from exc
    _swap_reports(stage_dir, output_dir / "reports")
    try:
        manifest_tmp.replace(manifest_path)
        readme_tmp.replace(readme)
    except OSError as exc:
        raise CloudBundleError(f"unable to install bundle manifest files: {exc}") from exc
    return {
        "mode": "cloud-export",
        "output_dir": str(output_dir),
        "manifest": str(manifest_path),
        "readme": str(readme),
        "included_reports": included_reports,
        "upload_ready": False,
    }


def load_bundle_manifest(bundle_dir: Path) -> dict[str, Any]:
    manifest_path = bundle_dir / "code-mower-cloud-bundle.json"
    if not manifest_path.is_file():
        raise CloudBundleError(f"bundle manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CloudBundleError(f"unable to read bundle manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != BUNDLE_SCHEMA:
        raise CloudBundleError(f"unsupported bundle manifest schema in {manifest_path}")
    return manifest


def _report_path_from_manifest(bundle_dir: Path, target: str) -> Path:
    if not target or target.startswith("/") or ".." in Path(target).parts:
        raise CloudBundleError(f"unsafe report target in bundle manifest: {target!r}")
    path = bundle_dir / target
    try:
        resolved = path.resolve()
        bundle_resolved = bundle_dir.resolve()
    except OSError as exc:
        raise CloudBundleError(f"unable to resolve bundle report path {path}: {exc}") from exc
    if not resolved.is_relative_to(bundle_resolved):
        raise CloudBundleError(f"report target escapes bundle directory: {target!r}")
    if not resolved.is_file():
        raise CloudBundleError(f"bundle report file is missing: {target!r}")
    return resolved


def _included_report_payloads(
    manifest: dict[str, Any],
    bundle_dir: Path,
    *,
    include_reports: bool,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for entry in manifest.get("included_reports", []):
        if not isinstance(entry, dict):
            raise CloudBundleError("bundle manifest has a non-object included_reports entry")
        target = str(entry.get("target", ""))
        report_payload = {
            "kind": entry.get("kind", ""),
            "target": target,
            "bytes": entry.get("bytes", 0),
            "source_basename": entry.get("source_basename", ""),
        }
        if include_reports:
            path = _report_path_from_manifest(bundle_dir, target)
            size = path.stat().st_size
            if size > MAX_REPORT_UPLOAD_BYTES:
                raise CloudBundleError(
                    f"refusing to upload {target}: {size} bytes exceeds "
                    f"{MAX_REPORT_UPLOAD_BYTES} byte limit"
                )
            try:
                report_payload["text"] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise CloudBundleError(f"report is not UTF-8 text: {target}") from exc
            except OSError as exc:
                raise CloudBundleError(f"unable to read report {target}: {exc}") from exc
        payloads.append(report_payload)
    return payloads


def build_upload_payload(
    *,
    bundle_dir: Path,
    include_reports: bool = False,
) -> dict[str, Any]:
    bundle_dir = bundle_dir.expanduser()
    if not bundle_dir.is_dir():
        raise CloudBundleError(f"bundle directory does not exist: {bundle_dir}")
    manifest = load_bundle_manifest(bundle_dir)
    return {
        "schema": UPLOAD_SCHEMA,
        "bundle_schema": manifest.get("schema"),
        "privacy_mode": manifest.get("privacy_mode", ""),
        "upload_mode": "reports_included" if include_reports else "metadata_only",
        "repo_slug": manifest.get("repo_slug", ""),
        "team_id": manifest.get("team_id", ""),
        "install_id": manifest.get("install_id", ""),
        "excluded_content": manifest.get("excluded_content", []),
        "reports": _included_report_payloads(
            manifest,
            bundle_dir,
            include_reports=include_reports,
        ),
        "notes": [
            "This upload payload is built from an explicit local bundle.",
            "Report contents are included only when --include-reports is set.",
        ],
    }


def post_upload_payload(
    *,
    payload: dict[str, Any],
    endpoint: str,
    token: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    parsed_endpoint = urllib.parse.urlparse(endpoint)
    is_local_http = (
        parsed_endpoint.scheme == "http"
        and parsed_endpoint.hostname in {"localhost", "127.0.0.1"}
    )
    if parsed_endpoint.scheme != "https" and not is_local_http:
        raise CloudBundleError(
            "upload endpoint must be https:// or a local development endpoint"
        )
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "code-mower-cloud-upload",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
            status = response.getcode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CloudBundleError(f"upload failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CloudBundleError(f"upload failed: {exc.reason}") from exc
    try:
        parsed = json.loads(response_body) if response_body else {}
    except json.JSONDecodeError as exc:
        raise CloudBundleError(f"upload response was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CloudBundleError("upload response JSON must be an object")
    return {
        "mode": "cloud-upload",
        "endpoint": endpoint,
        "status": status,
        "response": parsed,
    }


def render_bundle_readme(manifest: dict[str, Any]) -> str:
    lines = [
        "# Code Mower Cloud Benchmark Bundle",
        "",
        "This local bundle is the cloud-ready handoff shape for opt-in benchmark reporting.",
        "It is not uploaded by the OSS CLI.",
        "",
        "Excluded by default:",
    ]
    lines.extend(f"- {item}" for item in manifest["excluded_content"])
    lines.extend(["", "Included reports:"])
    if manifest["included_reports"]:
        lines.extend(
            f"- {entry['target']} ({entry['kind']}, {entry['bytes']} bytes)"
            for entry in manifest["included_reports"]
        )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Before sharing this bundle, inspect every file under `reports/`.",
            "Future upload support should require an explicit dry run and user confirmation.",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_report_args(values: list[str]) -> list[tuple[Path, str]]:
    reports: list[tuple[Path, str]] = []
    for raw in values:
        if "=" not in raw:
            raise CloudBundleError(
                "--report entries must use KIND=PATH, for example reviewer-metrics=metrics.json"
            )
        kind, path_text = raw.split("=", 1)
        reports.append((Path(path_text), _safe_kind(kind)))
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower cloud")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument(
        "--report",
        action="append",
        default=[],
        metavar="KIND=PATH",
        help=(
            "copy a shareable report into the bundle; kinds: "
            + ", ".join(sorted(SAFE_REPORT_KINDS))
        ),
    )
    export.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    export.add_argument("--repo-slug", default="")
    export.add_argument("--team-id", default="")
    export.add_argument("--install-id", default="")
    export.add_argument("--anonymous", action="store_true")
    export.add_argument("--json", action="store_true")
    upload = subparsers.add_parser("upload")
    upload.add_argument(
        "bundle_dir",
        nargs="?",
        type=Path,
        default=Path(DEFAULT_OUTPUT_DIR),
        help="bundle directory created by `code-mower cloud export`",
    )
    upload.add_argument(
        "--endpoint",
        default=os.environ.get("CODE_MOWER_CLOUD_ENDPOINT", DEFAULT_UPLOAD_ENDPOINT),
    )
    upload.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    upload.add_argument("--include-reports", action="store_true")
    upload.add_argument("--dry-run", action="store_true")
    upload.add_argument(
        "--yes",
        action="store_true",
        help="perform the network upload; without this, upload prints a dry-run preview",
    )
    upload.add_argument("--timeout", type=float, default=20.0)
    upload.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "export":
            payload = build_cloud_bundle(
                reports=_parse_report_args(args.report),
                output_dir=args.output_dir,
                repo_slug=args.repo_slug,
                team_id=args.team_id,
                install_id=args.install_id,
                anonymous=args.anonymous,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print("Code Mower cloud benchmark bundle")
                print(f"Output: {payload['output_dir']}")
                print(f"Manifest: {payload['manifest']}")
                print("Upload: local export only")
            return 0
        if args.command == "upload":
            payload = build_upload_payload(
                bundle_dir=args.bundle_dir,
                include_reports=args.include_reports,
            )
            dry_run = args.dry_run or not args.yes
            if dry_run:
                preview = {
                    "mode": "cloud-upload-dry-run",
                    "endpoint": args.endpoint,
                    "would_upload": False,
                    "requires_yes": not args.yes,
                    "upload_mode": payload["upload_mode"],
                    "report_count": len(payload["reports"]),
                    "privacy_mode": payload["privacy_mode"],
                    "excluded_content": payload["excluded_content"],
                }
                if args.json:
                    print(json.dumps(preview, indent=2, sort_keys=True))
                else:
                    print("Code Mower cloud upload dry run")
                    print(f"Endpoint: {preview['endpoint']}")
                    print(f"Mode: {preview['upload_mode']}")
                    print(f"Reports: {preview['report_count']}")
                    print("Network: skipped (pass --yes to upload)")
                return 0
            token = os.environ.get(args.token_env, "")
            result = post_upload_payload(
                payload=payload,
                endpoint=args.endpoint,
                token=token,
                timeout=args.timeout,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print("Code Mower cloud upload complete")
                print(f"Endpoint: {result['endpoint']}")
                print(f"Status: {result['status']}")
            return 0
    except CloudBundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled cloud command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
