"""CLI JSON protocol for Codex scheduled executions; no autonomous model calls."""
import argparse
import codecs
import json
from pathlib import Path
import sys
from .config import initialize_config, read_json
from . import __version__
from .runtime import Runtime


def _utf8_streams():
    """Keep the protocol UTF-8 when a Windows pipe defaults to a legacy code page.

    Otherwise a committed step, such as a new run lease, could be reported as a
    failure because its result cannot be printed.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if codecs.lookup(stream.encoding).name != "utf-8":
                stream.reconfigure(encoding="utf-8")
        except (AttributeError, LookupError, TypeError, ValueError):
            pass  # Detached streams and in-memory test buffers keep their own encoding.


def main(argv=None):
    _utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--lease-token")
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("init", help="Create a private profile; publication is disabled")
    cmd.add_argument("--workspace", type=Path, default=Path.cwd())
    cmd.add_argument("--output", type=Path)
    cmd.add_argument("--author", default="")
    for name in ("doctor", "status", "poll", "run-once", "next", "pause", "resume", "backend-health", "backend-identity", "bind-backend-account"):
        commands.add_parser(name)
    cmd = commands.add_parser("backend-management")
    cmd.add_argument("--state", choices=("all", "published", "pending_review", "rejected"), default="all")
    entry = commands.add_parser("import-url")
    entry.add_argument("--source-id", required=True)
    entry.add_argument("--url", required=True)
    for name in ("ingest-source", "source-outline", "draft", "review", "backend-review"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--job-id", required=True)
        cmd.add_argument("--input", type=Path, required=True)
    for name in ("plan-images", "image-intent", "image-result", "image-inspect", "image-block"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--job-id", required=True)
        cmd.add_argument("--input", type=Path, required=True)
    cmd = commands.add_parser("image-status")
    cmd.add_argument("--job-id", required=True)
    for name in ("acquire", "render", "backend-preflight", "backend-submit"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--job-id", required=True)
    cmd = commands.add_parser("prepare-publish")
    cmd.add_argument("--job-id", required=True)
    cmd.add_argument("--account-id", required=True)
    cmd.add_argument("--account-evidence", required=True)
    cmd = commands.add_parser("record-submit")
    cmd.add_argument("--attempt-id", required=True)
    cmd = commands.add_parser("reconcile")
    cmd.add_argument("--attempt-id", required=True)
    cmd.add_argument("--input", type=Path, required=True)
    cmd = commands.add_parser("backend-observations")
    cmd.add_argument("--attempt-id", required=True)
    cmd = commands.add_parser("backup")
    cmd.add_argument("--output", required=True)
    cmd = commands.add_parser("begin-run")
    cmd.add_argument("--owner", required=True)
    commands.add_parser("end-run")
    args = parser.parse_args(argv)
    runtime = None
    try:
        if args.command == "init":
            output = args.output or args.config or args.workspace / "config/xhs-automation.local.json"
            result = initialize_config(args.workspace, output, author=args.author)
            print(json.dumps({"success": True, **result}, ensure_ascii=False, indent=2))
            return 0
        if args.config is None:
            parser.error("--config is required for this command; create a profile with init first")
        runtime = Runtime(args.config, lease_token=args.lease_token)
        name = args.command
        if name == "status":
            result = runtime.store.status()
        elif name in ("doctor", "poll", "run-once", "next"):
            result = getattr(runtime, name.replace("-", "_"))()
        elif name == "backend-health":
            result = runtime.backend_health()
        elif name == "backend-management":
            result = runtime.backend_management(view=args.state)
        elif name in ("backend-identity", "bind-backend-account"):
            result = runtime.backend_identity(bind=name == "bind-backend-account")
        elif name == "import-url":
            result = runtime.import_url(args.source_id, args.url)
        elif name in ("ingest-source", "source-outline", "draft", "review", "backend-review", "plan-images", "image-intent", "image-result", "image-inspect", "image-block"):
            result = getattr(runtime, name.replace("-", "_"))(args.job_id, read_json(args.input))
        elif name == "image-status":
            result = runtime.image_status(args.job_id)
        elif name in ("acquire", "render", "backend-preflight", "backend-submit"):
            result = getattr(runtime, name.replace("-", "_"))(args.job_id)
        elif name == "backend-observations":
            result = runtime.backend_observations(args.attempt_id)
        elif name == "prepare-publish":
            result = runtime.prepare_publish(args.job_id, args.account_id, args.account_evidence)
        elif name == "record-submit":
            runtime.check_run()
            result = runtime.store.record_submit(args.attempt_id)
        elif name == "reconcile":
            result = runtime.reconcile(args.attempt_id, read_json(args.input))
        elif name in ("pause", "resume"):
            if name == "resume":
                runtime.check_run()
            runtime.store.set_paused(name == "pause")
            result = runtime.store.status()
        elif name == "backup":
            result = {"backup": runtime.store.backup(args.output)}
        elif name == "begin-run":
            result = runtime.store.begin_run(args.owner)
        elif name == "end-run":
            result = runtime.store.end_run(args.lease_token)
        print(json.dumps({"success": True, **result}, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"success": False, "error_type": type(error).__name__, "error": str(error)}, ensure_ascii=False))
        return 1
    finally:
        if runtime:
            runtime.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
