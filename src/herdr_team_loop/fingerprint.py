"""Snapshot real Git inputs, including changes hidden by index optimization flags."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from .errors import LoopError, require


def _git(repo: Path, *arguments: str) -> bytes:
    try:
        process = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *arguments],
                                 capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LoopError("git_unavailable", "Cannot read repository inputs", details={"repo": str(repo)}) from error
    require(process.returncode == 0, "invalid_repository", "Git input query failed",
            repo=str(repo), diagnostic=process.stderr.decode(errors="replace").strip())
    return process.stdout


def _file_bytes(digest, path: Path, repo: Path) -> None:
    # A replaced parent directory must not cause reads outside the selected tree.
    require(path.parent.resolve().is_relative_to(repo.resolve()), "invalid_repository",
            "Input path traverses outside the repository", path=str(path))
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        digest.update(b"missing")
        return
    require(stat.S_ISREG(mode) or stat.S_ISLNK(mode), "unsupported_input",
            "Unsupported repository input type", path=str(path))
    digest.update(str(mode).encode() + b"\0")
    if stat.S_ISLNK(mode):
        digest.update(os.fsencode(os.readlink(path)))
    else:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)


def _repository_digest(repo: Path, ancestors: tuple[Path, ...] = ()) -> str:
    require(repo.resolve() not in ancestors, "invalid_repository", "Cyclic submodule path")
    digest = hashlib.sha256(_git(repo, "rev-parse", "--verify", "HEAD^{commit}"))
    tracked = {}
    for metadata in (_git(repo, "ls-tree", "-r", "-z", "HEAD"), _git(repo, "ls-files", "--stage", "-z")):
        digest.update(metadata)
        for entry in filter(None, metadata.split(b"\0")):
            info, raw = entry.split(b"\t", 1)
            tracked[raw] = info.split()[0]
    for raw, mode in sorted(tracked.items()):
        path = repo / os.fsdecode(raw)
        digest.update(raw + b"\0")
        if mode == b"160000":
            require(not path.is_symlink(), "invalid_repository", "Submodule directory cannot be a symlink")
            if (path / ".git").exists():
                digest.update(_repository_digest(path, (*ancestors, repo.resolve())).encode())
            else:
                require(not path.exists() or (path.is_dir() and not any(path.iterdir())),
                        "invalid_repository", "Uninitialized submodule contains local files", path=str(path))
                digest.update(b"uninitialized-submodule")
        else:
            _file_bytes(digest, path, repo)
        digest.update(b"\0")
    for raw in sorted(filter(None, _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"))):
        digest.update(raw + b"\0")
        _file_bytes(digest, repo / os.fsdecode(raw), repo)
        digest.update(b"\0")
    return digest.hexdigest()


def fingerprint(repos: list[str], baseline: str = "master", scope_digest: str = "") -> dict:
    """Bind scope, baseline, HEAD, index and working contents without changing Git."""
    require(isinstance(repos, list) and all(isinstance(repo, str) and Path(repo).is_absolute() for repo in repos),
            "invalid_repository", "Repository paths must be absolute")
    require(isinstance(baseline, str) and baseline.strip() and "\0" not in baseline,
            "invalid_baseline", "A baseline ref is required")
    require(isinstance(scope_digest, str), "invalid_scope", "Scope digest must be a string")
    total = hashlib.sha256(scope_digest.encode() + b"\0")
    summaries = []
    try:
        for repo in sorted({Path(value).resolve() for value in repos}, key=str):
            require(_git(repo, "rev-parse", "--show-toplevel").decode().strip() == str(repo),
                    "invalid_repository", "Target must be an actual Git root", repo=str(repo))
            head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
            base = _git(repo, "rev-parse", "--verify", "--end-of-options", baseline + "^{commit}").decode().strip()
            item = {"repo": str(repo), "head": head, "baseline_sha": base,
                    "dirty_digest": _repository_digest(repo)}
            total.update(json.dumps(item, sort_keys=True).encode() + b"\0")
            summaries.append(item)
    except OSError as error:
        raise LoopError("input_unreadable", "Cannot read repository contents") from error
    return {"digest": total.hexdigest(), "repos": summaries}
