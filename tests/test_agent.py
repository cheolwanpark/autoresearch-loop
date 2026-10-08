from __future__ import annotations

from types import SimpleNamespace

from openai_codex.generated.v2_all import ThreadStartParams, ThreadResumeParams, TurnStartParams

from research_loop.agent import deny_request, run_agent
from research_loop.storage import read_json


class Payload:
    def __init__(self, data):
        self.data = data

    def model_dump(self, **kwargs):
        return self.data


class Client:
    calls = []

    def __init__(self, config, approval_handler):
        self.calls.append(("config", config))
        assert approval_handler("item/fileChange/requestApproval", {}) == {"decision": "decline"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def initialize(self):
        pass

    def thread_start(self, options):
        ThreadStartParams.model_validate(options)
        self.calls.append(("start", options))
        return SimpleNamespace(thread=SimpleNamespace(id="thread1"))

    def thread_resume(self, thread_id, options):
        ThreadResumeParams.model_validate({"threadId": thread_id, **options})
        self.calls.append(("resume", options))
        return SimpleNamespace(thread=SimpleNamespace(id=thread_id))

    def turn_start(self, thread_id, prompt, params):
        TurnStartParams.model_validate(
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt}], **params}
        )
        self.calls.append(("turn", params))
        return SimpleNamespace(turn=SimpleNamespace(id="turn1"))

    def next_turn_notification(self, turn_id):
        return SimpleNamespace(
            method="turn/completed",
            payload=Payload(
                {
                    "turn": {
                        "id": turn_id,
                        "status": "completed",
                        "items": [
                            {"type": "agentMessage", "text": "done", "phase": "final_answer"}
                        ],
                    }
                }
            ),
        )

    def unregister_turn_notifications(self, turn_id):
        pass

    def thread_read(self, thread_id, include_turns):
        return Payload(
            {
                "thread": {
                    "turns": [
                        {
                            "id": "turn1",
                            "status": "completed",
                            "items": [
                                {
                                    "type": "agentMessage",
                                    "text": "recovered",
                                    "phase": "final_answer",
                                }
                            ],
                        }
                    ]
                }
            }
        )


def test_sdk_wire_options_and_policies(tmp_path):
    Client.calls = []
    for role, model in (("worker", "gpt-6-luna"), ("coordinator", "gpt-6.1-sol")):
        cwd = tmp_path / role
        cwd.mkdir()
        for thread in (None, "existing"):
            result = run_agent(
                {
                    "cwd": str(cwd),
                    "role": role,
                    "model": model,
                    "instructions": "scope",
                    "prompt": "work",
                    "thread_id": thread,
                },
                cwd,
                Client,
            )
            assert result["text"] == "done"
            assert read_json(cwd / "agent.json")["turn_id"] == "turn1"
    for kind, options in Client.calls:
        if kind == "config":
            assert options.env["PATH"]
            assert options.env["GIT_TERMINAL_PROMPT"] == "0"
            continue
        assert options["approvalPolicy"] == "never"
        assert options["model"] in ("gpt-6-luna", "gpt-6.1-sol")
        worker = options["model"] == "gpt-6-luna"
        assert options["cwd"].endswith("worker" if worker else "coordinator")
        if kind == "turn":
            assert options["sandboxPolicy"]["type"] == ("workspaceWrite" if worker else "readOnly")
            if worker:
                assert options["sandboxPolicy"]["networkAccess"] is True
                assert options["sandboxPolicy"]["excludeSlashTmp"] is True
        else:
            assert options["sandbox"] == ("workspace-write" if worker else "read-only")


def test_recovered_completed_turn_not_executed_again(tmp_path):
    Client.calls = []
    result = run_agent(
        {
            "cwd": str(tmp_path),
            "role": "worker",
            "model": "specified",
            "instructions": "",
            "prompt": "",
            "thread_id": "thread1",
            "recover_turn": "turn1",
        },
        tmp_path,
        Client,
    )
    assert result["text"] == "recovered"
    assert not any(k == "turn" for k, _ in Client.calls)


def test_requests_never_wait_for_input():
    assert deny_request("item/commandExecution/requestApproval", {})["decision"] == "decline"
    assert deny_request("item/permissions/requestApproval", {})["permissions"] == {}
    answer = deny_request("item/tool/requestUserInput", {"questions": [{"id": "q"}]})
    assert "no human input" in answer["answers"]["q"]["answers"][0]
    assert deny_request("mcpServer/elicitation/request", {})["action"] == "decline"


def test_recover_turn_start_before_id_was_saved(tmp_path):
    Client.calls = []
    result = run_agent(
        {
            "cwd": str(tmp_path),
            "role": "worker",
            "model": "specified",
            "instructions": "",
            "prompt": "",
            "thread_id": "thread1",
            "recover": True,
            "prior_turn_ids": [],
        },
        tmp_path,
        Client,
    )
    assert result["text"] == "recovered"
    assert not any(k == "turn" for k, _ in Client.calls)


def test_private_codex_home_and_auth_copy(tmp_path, monkeypatch):
    original = tmp_path / "original-codex"
    original.mkdir()
    (original / "auth.json").write_text('{"test": "fake credential"}')
    (original / "config.toml").write_text('danger = "do not inherit"')
    cwd = tmp_path / "worker"
    cwd.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(original))
    Client.calls = []
    run_agent(
        {"cwd": str(cwd), "role": "worker", "model": "specified", "instructions": "", "prompt": ""},
        cwd,
        Client,
    )
    config = Client.calls[0][1]
    from pathlib import Path

    private = Path(config.env["CODEX_HOME"])
    assert private.is_relative_to(cwd)
    assert (private / "auth.json").read_text() == (original / "auth.json").read_text()
    assert (private / "auth.json").stat().st_mode & 0o777 == 0o600
    assert not (private / "config.toml").exists()


def test_new_login_refreshes_local_credentials_without_clobbering_local_refresh(
    tmp_path, monkeypatch
):
    original = tmp_path / "auth-source"
    original.mkdir()
    source = original / "auth.json"
    source.write_text('{"token": "initial"}')
    monkeypatch.setenv("CODEX_HOME", str(original))
    cwd = tmp_path / "work"
    cwd.mkdir()
    request = {
        "cwd": str(cwd),
        "role": "worker",
        "model": "specified",
        "instructions": "",
        "prompt": "",
    }
    run_agent(request, cwd, Client)
    local = cwd / ".research-local" / "codex" / "auth.json"
    local.write_text('{"token": "locally-refreshed"}')
    run_agent(request, cwd, Client)
    assert "locally-refreshed" in local.read_text()
    source.write_text('{"token": "new-login"}')
    run_agent(request, cwd, Client)
    assert local.read_text() == source.read_text()
