"""Pubblicazione del report su GitHub Pages tramite le API REST di GitHub (solo libreria standard).

Il ramo di destinazione contiene un unico commit senza genitori, sostituito a ogni
pubblicazione: il repository non cresce di un report al giorno.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from . import config

API = "https://api.github.com"
REPO_RE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _api(token: str, method: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": config.USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("message", "")
        except ValueError:
            message = ""
        raise GitHubError(exc.code, f"GitHub {method} {path}: HTTP {exc.code} {message}".rstrip()) from None


def publish_pages(html: Path, repo: str, token: str, branch: str = "gh-pages") -> str:
    """Sostituisce il contenuto di ``branch`` con ``html`` come ``index.html``; restituisce lo sha del commit."""
    if not REPO_RE.fullmatch(repo):
        raise ValueError(f"repository non valido: {repo!r} (atteso proprietario/nome)")
    if not BRANCH_RE.fullmatch(branch) or ".." in branch:
        raise ValueError(f"ramo non valido: {branch!r}")
    content = base64.b64encode(html.read_bytes()).decode()
    git = f"/repos/{repo}/git"
    user = _api(token, "GET", "/user")
    # indirizzo noreply di GitHub: l'email dell'account non finisce nel commit
    who = {"name": user["login"], "email": f"{user['id']}+{user['login']}@users.noreply.github.com"}
    blob = _api(token, "POST", f"{git}/blobs", {"content": content, "encoding": "base64"})
    tree = _api(token, "POST", f"{git}/trees", {"tree": [
        {"path": "index.html", "mode": "100644", "type": "blob", "sha": blob["sha"]},
        # niente build Jekyll: la pagina è già pronta
        {"path": ".nojekyll", "mode": "100644", "type": "blob", "content": ""},
    ]})
    commit = _api(token, "POST", f"{git}/commits", {
        "message": f"Report del {date.today():%d/%m/%Y}",
        "tree": tree["sha"], "parents": [], "author": who, "committer": who,
    })
    try:
        _api(token, "PATCH", f"{git}/refs/heads/{branch}", {"sha": commit["sha"], "force": True})
    except GitHubError as exc:
        if exc.status != 422:  # 422: il ramo non esiste ancora
            raise
        _api(token, "POST", f"{git}/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    return commit["sha"]
