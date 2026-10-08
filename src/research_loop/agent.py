"""The sole SDK boundary, pinned to openai-codex 0.161.0's typed RPC client.

The lower-level client exposes approval handlers and full sandbox policies;
the high-level preset API cannot express network-enabled workspace turns.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from openai_codex.client import CodexClient, CodexConfig

from .storage import atomic_json, read_json
from .workspace import environment

UNATTENDED = "Unattended campaign: no human input is available. Choose a safe alternative within the assigned scope. Never wait for approval or send messages."


def deny_request(method: str, params: dict | None) -> dict:
    params = params or {}
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        return {"decision": "decline"}
    if method in ("execCommandApproval", "applyPatchApproval"):
        return {"decision": "denied"}
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "turn"}
    if method == "item/tool/requestUserInput":
        return {
            "answers": {q["id"]: {"answers": [UNATTENDED]} for q in params.get("questions", [])}
        }
    if "elicitation" in method.lower():
        return {"action": "decline"}
    # Dynamic tools and unknown requests receive an immediate negative response.
    return {"success": False, "contentItems": [{"type": "inputText", "text": UNATTENDED}]}


def final_text(turn: dict) -> str:
    messages = [x for x in turn.get("items", []) if x.get("type") == "agentMessage"]
    finals = [x for x in messages if x.get("phase") == "final_answer"]
    return (finals or messages or [{}])[-1].get("text", "")


def run_agent(request: dict, path: Path, client_factory=CodexClient) -> dict:
    cwd = Path(request["cwd"])
    worker = request["role"] == "worker"
    sandbox = "workspace-write" if worker else "read-only"
    env = environment(cwd)
    # Keep sessions/config/cache writes out of the user's home. Copy only file
    # credentials, never global config, plugins, hooks or notification commands.
    codex_home = cwd / ".research-local" / "codex"
    codex_home.mkdir(mode=0o700, exist_ok=True)
    source_auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    local_auth = codex_home / "auth.json"
    fingerprint_file = codex_home / "source-auth.json"
    if source_auth.is_file():
        fingerprint = hashlib.sha256(source_auth.read_bytes()).hexdigest()
        previous = read_json(fingerprint_file) if fingerprint_file.exists() else None
        if not local_auth.exists() or previous != fingerprint:
            shutil.copyfile(source_auth, local_auth)
            local_auth.chmod(0o600)
            atomic_json(fingerprint_file, fingerprint)
    env["CODEX_HOME"] = str(codex_home)
    config = CodexConfig(
        cwd=str(cwd),
        env=env,
        config_overrides=(
            'approval_policy="never"',
            f'sandbox_mode="{sandbox}"',
            'cli_auth_credentials_store="file"',
            "sandbox_workspace_write.network_access=true",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        ),
    )
    options = {
        "model": request["model"],
        "cwd": str(cwd),
        "approvalPolicy": "never",
        "sandbox": sandbox,
        "config": {
            "approval_policy": "never",
            "sandbox_workspace_write": {
                "network_access": True,
                "writable_roots": [],
                "exclude_slash_tmp": True,
                "exclude_tmpdir_env_var": True,
            },
        },
        "developerInstructions": UNATTENDED + "\n" + request["instructions"],
    }
    with client_factory(config=config, approval_handler=deny_request) as client:
        client.initialize()
        if request.get("thread_id"):
            response = client.thread_resume(request["thread_id"], options)
        else:
            response = client.thread_start(options)
        thread_id = response.thread.id
        metadata = {"thread_id": thread_id, "model": request["model"], "role": request["role"]}
        history = None
        if request.get("thread_id"):
            history = client.thread_read(thread_id, include_turns=True).model_dump(
                mode="json", by_alias=True
            )
        if request.get("recover") or request.get("recover_turn"):
            atomic_json(path / "recovered-thread.json", history)
            for turn in reversed(history["thread"].get("turns", []) if history else []):
                target = request.get("recover_turn")
                matching = (
                    turn["id"] == target
                    if target
                    else turn["id"] not in request.get("prior_turn_ids", [])
                )
                if matching and turn["status"] == "completed":
                    atomic_json(path / "agent.json", metadata | {"turn_id": turn["id"]})
                    return metadata | {
                        "turn_id": turn["id"],
                        "text": final_text(turn),
                        "recovered": True,
                    }
            # The old process group is confirmed dead before this recovery job.
        metadata["prior_turn_ids"] = (
            [t["id"] for t in history["thread"].get("turns", [])] if history else []
        )
        # Save the existing turn IDs before sending turn/start. This also covers
        # a crash after the server accepts a turn but before its ID is saved.
        atomic_json(path / "agent.json", metadata)
        policy = (
            {
                "type": "workspaceWrite",
                "writableRoots": [str(cwd)],
                "networkAccess": True,
                "excludeSlashTmp": True,
                "excludeTmpdirEnvVar": True,
            }
            if worker
            else {"type": "readOnly"}
        )
        turn = client.turn_start(
            thread_id,
            request["prompt"],
            params={
                "approvalPolicy": "never",
                "model": request["model"],
                "cwd": str(cwd),
                "sandboxPolicy": policy,
            },
        )
        turn_id = turn.turn.id
        atomic_json(path / "agent.json", metadata | {"turn_id": turn_id})
        text = ""
        with (path / "events.jsonl").open("a") as log:
            try:
                while True:
                    event = client.next_turn_notification(turn_id)
                    payload = event.payload.model_dump(mode="json", by_alias=True)
                    log.write(json.dumps({"method": event.method, "payload": payload}) + "\n")
                    log.flush()
                    if event.method == "item/completed":
                        item = payload.get("item", {})
                        if item.get("type") == "agentMessage":
                            text = item.get("text", "")
                    if event.method == "turn/completed":
                        completed = payload["turn"]
                        if completed["status"] != "completed":
                            raise RuntimeError(json.dumps(completed.get("error") or completed))
                        return metadata | {
                            "turn_id": turn_id,
                            "text": final_text(completed) or text,
                        }
            finally:
                client.unregister_turn_notifications(turn_id)
