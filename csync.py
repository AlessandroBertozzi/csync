#!/usr/bin/env python3
"""csync - sync Claude Code conversations to a remote (via rclone) and pull them
back into any local project directory.

Claude Code stores sessions in ~/.claude/projects/<encoded-path>/*.jsonl where
<encoded-path> is the absolute project path with every non-alphanumeric
character replaced by "-".  csync pushes those folders to an rclone remote and
pulls them back under the encoding of whatever local path you choose, so that
`claude --resume` run inside that path finds them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

__version__ = "0.1.0"

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
PROJECTS_DIR = CLAUDE_DIR / "projects"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "csync" / "config.toml"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "csync"
INDEX_NAME = "csync-index.json"
WORKTREE_MARKER = "-claude-worktrees-"


# --------------------------------------------------------------------------- utils

class CsyncError(Exception):
    pass


def encode_path(path: str | Path) -> str:
    """Encode an absolute path the way Claude Code names project folders."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        import tomllib
    except ImportError:  # pragma: no cover - python < 3.11
        raise CsyncError("Python >= 3.11 is required (tomllib)")
    with CONFIG_FILE.open("rb") as fh:
        return tomllib.load(fh)


def resolve_remote(cli_value: str | None) -> str:
    remote = cli_value or os.environ.get("CSYNC_REMOTE") or load_config().get("remote")
    if not remote:
        raise CsyncError(
            "no remote configured: pass --remote, set CSYNC_REMOTE, or write\n"
            f'  remote = "gdrive:claude-sessions"\n'
            f"into {CONFIG_FILE}"
        )
    return remote.rstrip("/")


def rclone(args: list[str], *, capture: bool = False, check: bool = True,
           quiet: bool = False) -> subprocess.CompletedProcess:
    if shutil.which("rclone") is None:
        raise CsyncError("rclone not found in PATH (https://rclone.org/install/)")
    cmd = ["rclone", *args]
    if quiet and not capture:
        cmd.append("--quiet")
    elif "--dry-run" not in args and "--log-level" not in args:
        cmd += ["--log-level", "ERROR"]
    proc = subprocess.run(cmd, text=True, capture_output=capture)
    if check and proc.returncode != 0:
        msg = proc.stderr.strip() if capture else ""
        raise CsyncError(f"rclone failed ({' '.join(args[:2])}): {msg}")
    return proc


def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


def session_files(project_dir: Path) -> list[Path]:
    return sorted(project_dir.rglob("*.jsonl"))


def detect_original_path(project_dir: Path) -> str | None:
    """Read the project root path from the sessions' `cwd` fields.

    Returns the cwd whose encoding matches the folder name (the project root),
    or None when no session carries a cwd.
    """
    candidates: set[str] = set()
    for f in session_files(project_dir)[:20]:
        try:
            with f.open(errors="ignore") as fh:
                for i, line in enumerate(fh):
                    if i > 200:
                        break
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd")
                    if isinstance(cwd, str):
                        candidates.add(cwd)
        except OSError:
            continue
    for cwd in sorted(candidates, key=len):
        if encode_path(cwd) == project_dir.name:
            return cwd
    return None


# --------------------------------------------------------------------------- index

def read_index(remote: str) -> dict:
    proc = rclone(["cat", f"{remote}/{INDEX_NAME}"], capture=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def write_index(remote: str, index: dict) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(index, tmp, indent=2, sort_keys=True)
        tmp_path = tmp.name
    try:
        rclone(["copyto", tmp_path, f"{remote}/{INDEX_NAME}"], capture=True)
    finally:
        os.unlink(tmp_path)


# --------------------------------------------------------------------------- push

def session_units(project_dir: Path) -> list[tuple[str, Path, Path | None]]:
    """A session is `<id>.jsonl` plus an optional companion dir `<id>/`
    (tool results, subagents, custom title). Returns (id, jsonl, dir|None)."""
    units = []
    for f in sorted(project_dir.glob("*.jsonl")):
        sid = f.stem
        comp = project_dir / sid
        units.append((sid, f, comp if comp.is_dir() else None))
    return units


def collect_files(project_dir: Path, since_days: int | None) -> list[str]:
    """Relative paths (to PROJECTS_DIR) of everything to push for one project."""
    cutoff = time.time() - since_days * 86400 if since_days is not None else None
    rel = lambda p: str(p.relative_to(PROJECTS_DIR))
    out: list[str] = []
    for _sid, jsonl, comp in session_units(project_dir):
        if cutoff is not None and jsonl.stat().st_mtime < cutoff:
            continue
        out.append(rel(jsonl))
        if comp:
            out += [rel(p) for p in comp.rglob("*") if p.is_file()]
    memory = project_dir / "memory"
    if memory.is_dir():
        out += [rel(p) for p in memory.rglob("*") if p.is_file()]
    return out


def cmd_push(ns: argparse.Namespace) -> int:
    remote = resolve_remote(ns.remote)
    if ns.hook:
        return hook_push(ns)

    if ns.all:
        dirs = [d for d in sorted(PROJECTS_DIR.iterdir()) if d.is_dir()]
        if not ns.include_worktrees:
            dirs = [d for d in dirs if WORKTREE_MARKER not in d.name]
    else:
        target = Path(ns.path or os.getcwd()).resolve()
        d = PROJECTS_DIR / encode_path(target)
        if not d.is_dir():
            raise CsyncError(f"no Claude project folder for {target} (expected {d})")
        dirs = [d]

    files: list[str] = []
    touched: list[Path] = []
    total_sessions = 0
    for d in dirs:
        chunk = collect_files(d, ns.since)
        if chunk:
            n_sess = sum(1 for f in chunk if f.endswith(".jsonl") and f.count("/") == 1)
            total_sessions += n_sess
            files += chunk
            touched.append(d)
            log(f"push {d.name}: {n_sess} session(s), {len(chunk)} file(s)", ns.quiet)
    if not files:
        log("nothing to push", ns.quiet)
        return 0
    if total_sessions == 0:
        log(f"warning: no session matches --since {ns.since}; only memory files will be pushed",
            ns.quiet)

    with tempfile.NamedTemporaryFile("w", suffix=".lst", delete=False) as tmp:
        tmp.write("\n".join(files) + "\n")
        list_path = tmp.name
    try:
        args = ["copy", str(PROJECTS_DIR), remote, "--files-from-raw", list_path]
        if ns.dry_run:
            args.append("--dry-run")
        rclone(args, quiet=ns.quiet)
    finally:
        os.unlink(list_path)

    if not ns.dry_run:
        index = read_index(remote)
        for d in touched:
            index[d.name] = {
                "path": detect_original_path(d) or index.get(d.name, {}).get("path"),
                "host": os.uname().nodename,
                "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "sessions": len(session_units(d)),
            }
        write_index(remote, index)
    log(f"done: {len(touched)} project(s), {total_sessions} session(s), {len(files)} file(s)",
        ns.quiet)
    return 0


def hook_push(ns: argparse.Namespace) -> int:
    """Called from a Claude Code SessionEnd hook: read the hook JSON from stdin
    and push the session's project in a detached background process so the
    session exit is not delayed."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        payload = {}
    cwd = payload.get("cwd") or os.getcwd()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logfile = (STATE_DIR / "hook.log").open("a")
    logfile.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} push {cwd}\n")
    logfile.flush()
    cmd = [sys.executable, "-m", "csync", "push", cwd, "--quiet"]
    if ns.remote:
        cmd += ["--remote", ns.remote]
    subprocess.Popen(cmd, stdout=logfile, stderr=logfile, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    return 0


# --------------------------------------------------------------------------- list

def remote_files(remote: str) -> list[dict]:
    proc = rclone(["lsjson", "-R", "--files-only", remote], capture=True)
    return json.loads(proc.stdout or "[]")


def remote_projects(remote: str, entries: list[dict] | None = None) -> list[dict]:
    """Return [{name, sessions, last}] for every project folder on the remote."""
    rows: dict[str, dict] = {}
    for entry in entries if entries is not None else remote_files(remote):
        path = entry["Path"]
        if "/" not in path:
            continue
        name, rest = path.split("/", 1)
        row = rows.setdefault(name, {"name": name, "sessions": 0, "last": ""})
        if rest.endswith(".jsonl") and "/" not in rest:
            row["sessions"] += 1
            row["last"] = max(row["last"], entry.get("ModTime", "")[:19])
    for row in rows.values():
        row["last"] = to_local(row["last"])
    return sorted(rows.values(), key=lambda r: r["last"], reverse=True)


def to_local(iso_utc: str) -> str:
    """rclone reports ModTime in UTC; show it in local time."""
    if not iso_utc:
        return ""
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(iso_utc[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return iso_utc


def cmd_list(ns: argparse.Namespace) -> int:
    remote = resolve_remote(ns.remote)
    index = read_index(remote)
    rows = remote_projects(remote)
    for r in rows:
        meta = index.get(r["name"], {})
        r["path"] = meta.get("path")
        r["host"] = meta.get("host")
    if ns.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(f"no projects on {remote}")
        return 0
    width = max(len(r["name"]) for r in rows)
    print(f"{'PROJECT':<{width}}  SESSIONS  LAST                 PATH")
    for r in rows:
        print(f"{r['name']:<{width}}  {r['sessions']:>8}  {r['last'] or '-':<19}  {r['path'] or '?'}")
    return 0


# --------------------------------------------------------------------------- pull

def resolve_project(remote: str, query: str, index: dict, entries: list[dict]) -> str:
    names = [r["name"] for r in remote_projects(remote, entries)]
    if query in names:
        return query
    by_path = {meta.get("path"): name for name, meta in index.items() if meta.get("path")}
    q_abs = str(Path(query).expanduser().resolve()) if query.startswith(("/", "~", ".")) else None
    if q_abs and q_abs in by_path and by_path[q_abs] in names:
        return by_path[q_abs]
    if q_abs and encode_path(q_abs) in names:
        return encode_path(q_abs)
    ql = query.lower()
    matches = [n for n in names
               if ql in n.lower() or ql in (index.get(n, {}).get("path") or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CsyncError(f"no remote project matches {query!r} (see `csync list`)")
    raise CsyncError("ambiguous project, candidates:\n  " + "\n  ".join(matches))


def select_remote_files(entries: list[dict], name: str, since_days: int | None) -> list[str]:
    """Files (relative to the remote project folder) to pull: sessions newer than
    `since_days` with their companion dirs, plus memory."""
    prefix = name + "/"
    files = [(e["Path"][len(prefix):], e.get("ModTime", "")) for e in entries
             if e["Path"].startswith(prefix)]
    if since_days is None:
        return [p for p, _ in files]
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - since_days * 86400))
    keep_ids = {p[:-6] for p, mod in files if p.endswith(".jsonl") and "/" not in p and mod[:19] >= cutoff}
    return [p for p, _ in files
            if p.startswith("memory/") or p[:-6] in keep_ids and p.endswith(".jsonl") and "/" not in p
            or p.split("/", 1)[0] in keep_ids]


def rewrite_paths(target_dir: Path, old: str, new: str, quiet: bool) -> int:
    if old == new:
        return 0
    old_b, new_b = old.encode(), new.encode()
    changed = 0
    for f in session_files(target_dir):
        data = f.read_bytes()
        if old_b in data:
            f.write_bytes(data.replace(old_b, new_b))
            changed += 1
    log(f"rewrote {old} -> {new} in {changed} file(s)", quiet)
    return changed


def cmd_pull(ns: argparse.Namespace) -> int:
    remote = resolve_remote(ns.remote)
    index = read_index(remote)
    entries = remote_files(remote)
    name = resolve_project(remote, ns.project, index, entries)
    meta = index.get(name, {})

    if ns.to:
        local_path = Path(ns.to).expanduser().resolve()
    elif meta.get("path"):
        local_path = Path(meta["path"])
    else:
        raise CsyncError("original path unknown for this project: pass --to <local-dir>")
    target_dir = PROJECTS_DIR / encode_path(local_path)


    wanted = select_remote_files(entries, name, ns.since)
    n_sess = sum(1 for p in wanted if p.endswith(".jsonl") and "/" not in p)
    if not wanted:
        raise CsyncError("nothing to pull (check --since)")
    if n_sess == 0:
        log("warning: no session selected (check --since or `csync list`); pulling memory only",
            ns.quiet)
    with tempfile.NamedTemporaryFile("w", suffix=".lst", delete=False) as tmp:
        tmp.write("\n".join(wanted) + "\n")
        list_path = tmp.name
    try:
        args = ["copy", f"{remote}/{name}", str(target_dir), "--files-from-raw", list_path]
        if ns.dry_run:
            args.append("--dry-run")
        log(f"pull {name}: {n_sess} session(s), {len(wanted)} file(s) -> {target_dir}", ns.quiet)
        rclone(args, quiet=ns.quiet)
    finally:
        os.unlink(list_path)

    if ns.rewrite_paths and not ns.dry_run:
        old = meta.get("path") or detect_original_path(target_dir)
        if not old:
            log("warning: cannot rewrite paths, original path unknown", ns.quiet)
        else:
            rewrite_paths(target_dir, old, str(local_path), ns.quiet)
    prefix = "" if local_path.exists() else f"mkdir -p {local_path} && "
    log(f"done. Resume with:\n  {prefix}cd {local_path} && claude --resume", ns.quiet)
    return 0


# --------------------------------------------------------------------------- rm

def cmd_rm(ns: argparse.Namespace) -> int:
    remote = resolve_remote(ns.remote)
    index = read_index(remote)
    entries = remote_files(remote)
    name = resolve_project(remote, ns.project, index, entries)
    prefix = name + "/"
    n_files = sum(1 for e in entries if e["Path"].startswith(prefix))
    n_sess = sum(1 for e in entries if e["Path"].startswith(prefix)
                 and e["Path"].endswith(".jsonl") and e["Path"].count("/") == 1)
    path = index.get(name, {}).get("path") or "?"
    print(f"{name}\n  path: {path}\n  {n_sess} session(s), {n_files} file(s) on {remote}", file=sys.stderr)
    if not ns.yes:
        if not sys.stdin.isatty():
            raise CsyncError("refusing to delete without --yes in a non-interactive shell")
        answer = input("delete from remote? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            log("aborted", False)
            return 1
    rclone(["purge", f"{remote}/{name}"], quiet=ns.quiet)
    if name in index:
        del index[name]
        write_index(remote, index)
    log(f"deleted {name} from {remote} (local sessions untouched)", ns.quiet)
    return 0


# --------------------------------------------------------------------------- misc

HOOK_SNIPPET = {
    "hooks": {
        "SessionEnd": [
            {"hooks": [{"type": "command", "command": "csync push --hook"}]}
        ]
    }
}


def cmd_hook_snippet(ns: argparse.Namespace) -> int:
    print(json.dumps(HOOK_SNIPPET, indent=2))
    print(f"\nMerge the above into {CLAUDE_DIR / 'settings.json'} to push every session "
          "automatically when it ends.", file=sys.stderr)
    return 0


def cmd_doctor(ns: argparse.Namespace) -> int:
    ok = True

    def check(label: str, good: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"[{'ok' if good else '!!'}] {label}" + (f"  -> {hint}" if not good and hint else ""))

    check("rclone in PATH", shutil.which("rclone") is not None, "https://rclone.org/install/")
    check(f"projects dir {PROJECTS_DIR}", PROJECTS_DIR.is_dir())
    try:
        remote = resolve_remote(ns.remote)
        check(f"remote configured ({remote})", True)
        proc = rclone(["lsd", remote], capture=True, check=False)
        err = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
        if proc.returncode != 0 and "directory not found" in err:
            check("remote reachable (empty, created on first push)", True)
        else:
            check("remote reachable", proc.returncode == 0, err)
    except CsyncError as e:
        check("remote configured", False, str(e).splitlines()[0])
    return 0 if ok else 1


# --------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="csync", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"csync {__version__}")
    p.add_argument("--remote", help="rclone remote path, e.g. gdrive:claude-sessions "
                                    "(default: $CSYNC_REMOTE or config file)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("push", help="upload a project's sessions to the remote")
    s.add_argument("path", nargs="?", help="project directory (default: cwd)")
    s.add_argument("--all", action="store_true", help="push every local project")
    s.add_argument("--include-worktrees", action="store_true",
                   help="with --all, also push .claude/worktrees projects")
    s.add_argument("--since", type=int, metavar="DAYS", help="only sessions modified in the last DAYS")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--hook", action="store_true",
                   help="SessionEnd hook mode: read hook JSON on stdin, push in background")
    s.set_defaults(func=cmd_push)

    s = sub.add_parser("list", help="list projects available on the remote")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("pull", help="download a project's sessions into a local project dir")
    s.add_argument("project", help="remote project name, original path, or unique substring")
    s.add_argument("--to", metavar="DIR", help="local project directory to attach the sessions to "
                                              "(default: the original path)")
    s.add_argument("--rewrite-paths", action="store_true",
                   help="replace the original absolute path with DIR inside the session files")
    s.add_argument("--since", type=int, metavar="DAYS")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("rm", help="delete a project's sessions from the remote (local files untouched)")
    s.add_argument("project", help="remote project name, original path, or unique substring")
    s.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("hook-snippet", help="print the settings.json hook for automatic push")
    s.set_defaults(func=cmd_hook_snippet)

    s = sub.add_parser("doctor", help="check rclone, remote and local layout")
    s.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        return ns.func(ns)
    except CsyncError as e:
        print(f"csync: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
