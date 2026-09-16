from __future__ import annotations

import json
import runpy
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modal_devin import cli
from modal_devin._client import OutpostsClient


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


class RecordingUrlOpen:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        body = self.bodies.pop(0)
        if isinstance(body, BaseException):
            raise body
        return Response(json.dumps(body).encode())


def _fake_outposts_client(recorder):
    """A cli.OutpostsClient replacement that routes real HTTP construction through a recorder."""

    def factory(base_url, token, *, timeout):
        return OutpostsClient(base_url, token, timeout=timeout, urlopen=recorder)

    return factory


def initialize(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    cli.init_worker(
        name="demo-pool",
        outpost_id="outpost_env-demo",
        outposts_dir=tmp_path,
        **kwargs,
    )
    return tmp_path / "demo_pool.py"


def test_init_generates_an_owned_editable_modal_app(tmp_path, monkeypatch):
    generated = initialize(tmp_path, monkeypatch)

    source = generated.read_text()
    namespace = runpy.run_path(str(generated))

    assert len(source.splitlines()) <= 60
    assert "Generated once by modal-devin" in source
    assert "import modal" in source
    assert "from modal_devin import Worker" in source
    assert "worker = Worker.from_env(" in source
    assert "app = modal.App(" in source
    assert "devin_secret = modal.Secret.from_name(" in source
    assert "controller_image = worker.controller_image()" in source
    assert "image = worker.prepare_image(base_image)" in source
    assert source.count("@app.function(") == 2
    assert "schedule=modal.Period(" in source
    assert "max_containers=1" in source
    assert 'name="scheduler",\n    image=controller_image,' in source
    assert "worker.run_session(" in source
    assert "worker.dispatch_pending_sessions(session.spawn)" in source
    assert "worker.app(" not in source
    assert set(namespace["app"].registered_functions) == {"scheduler", "session"}


def test_generated_app_accepts_awkward_human_name_but_uses_safe_resources(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"sec]ret"})

    cli.init_worker(
        name='bad/name "✨"',
        outpost_id='outpost_env-"quoted"',
        api_url="https://api.example.com",
        secret_name="sec]ret",
        outposts_dir=tmp_path,
    )

    generated = tmp_path / "bad_name.py"
    namespace = runpy.run_path(str(generated))
    worker = namespace["worker"]
    assert "/" not in worker.app_name
    assert namespace["app"].name == worker.app_name
    assert len(namespace["app"].name) < 64


def test_generated_app_reads_typed_worker_settings_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MODAL_DEVIN_SCHEDULER_INTERVAL_SECONDS", "17")
    monkeypatch.setenv("MODAL_DEVIN_SESSION_TIMEOUT_SECONDS", "900")
    generated = initialize(tmp_path, monkeypatch)

    worker = runpy.run_path(str(generated))["worker"]

    assert worker.settings.scheduler_interval_seconds == 17
    assert worker.settings.session_timeout_seconds == 900


def test_generated_app_imports_without_remote_calls(tmp_path, monkeypatch):
    generated = initialize(tmp_path, monkeypatch)

    runpy.run_path(str(generated))


def test_create_modal_secret_uses_the_sdk_without_a_subprocess(monkeypatch):
    create = Mock()
    monkeypatch.setattr(
        cli.modal,
        "Secret",
        SimpleNamespace(objects=SimpleNamespace(create=create)),
    )
    monkeypatch.setattr(
        cli,
        "_modal",
        lambda *args: (_ for _ in ()).throw(AssertionError("secret subprocess")),
    )

    cli._create_modal_secret("devin-outposts-token", "super-secret-token")

    create.assert_called_once_with(
        "devin-outposts-token",
        {"DEVIN_OUTPOSTS_TOKEN": "super-secret-token"},
    )


def test_init_can_deploy_from_the_project_environment(tmp_path, monkeypatch):
    deployed = []
    monkeypatch.setattr(cli, "_modal_deploy", lambda path: deployed.append(path) or 0)

    generated = initialize(tmp_path, monkeypatch, deploy=True)

    assert deployed == [generated]


@pytest.mark.parametrize("interactive", [False, True])
def test_modal_deploy_always_uses_a_rolling_strategy(tmp_path, monkeypatch, interactive):
    outpost_file = tmp_path / "worker.py"
    expected = [
        sys.executable,
        "-m",
        "modal",
        "deploy",
        "--strategy",
        "rolling",
        str(outpost_file),
    ]
    monkeypatch.setattr(cli, "_interactive", lambda: interactive)
    run_with_tail = Mock(return_value=0)
    run = Mock(return_value=SimpleNamespace(returncode=0))

    if interactive:
        monkeypatch.setattr(cli, "_run_with_tail", run_with_tail)
    else:
        monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli._modal_deploy(outpost_file) == 0

    if interactive:
        run_with_tail.assert_called_once_with(expected)
    else:
        run.assert_called_once_with(expected)


def test_deploy_propagates_modal_exit_code(monkeypatch):
    monkeypatch.setattr(cli, "_modal_deploy", lambda path: 23)

    with pytest.raises(SystemExit) as exc_info:
        cli.deploy(Path("worker.py"))

    assert exc_info.value.code == 23


def test_deploy_without_a_file_deploys_every_outpost_in_the_directory(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    deployed = []
    monkeypatch.setattr(cli, "_modal_deploy", lambda path: deployed.append(path) or 0)

    cli.deploy(outposts_dir=tmp_path)

    assert deployed == sorted(tmp_path.glob("*.py"))


def test_deploy_without_a_file_reports_which_outposts_failed(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.py").write_text("")
    attempted = []

    def fake_deploy(path):
        attempted.append(path)
        return 0 if path.name != "b.py" else 1

    monkeypatch.setattr(cli, "_modal_deploy", fake_deploy)

    with pytest.raises(SystemExit, match=r"1 of 3 outpost\(s\) failed to deploy.*b\.py"):
        cli.deploy(outposts_dir=tmp_path)

    assert attempted == sorted(tmp_path.glob("*.py"))


def test_deploy_without_a_file_or_outposts_reports_nothing_to_deploy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_modal_deploy",
        lambda path: (_ for _ in ()).throw(AssertionError("nothing to deploy")),
    )

    with pytest.raises(SystemExit, match="No outpost files found"):
        cli.deploy(outposts_dir=tmp_path)


def test_destroy_stops_the_modal_app_then_deletes_the_outpost(tmp_path, monkeypatch):
    generated = initialize(tmp_path, monkeypatch)
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "super-secret-token")
    lookup = Mock(return_value=None)
    monkeypatch.setattr(cli.modal, "App", SimpleNamespace(lookup=lookup))
    stopped = []
    monkeypatch.setattr(
        cli,
        "_modal_stop",
        lambda name: stopped.append(name) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    recorder = RecordingUrlOpen([{}])
    monkeypatch.setattr(cli, "OutpostsClient", _fake_outposts_client(recorder))

    cli.destroy(generated, yes=True)

    lookup.assert_called_once_with("modal-devin-demo-pool", create_if_missing=False)
    assert stopped == ["modal-devin-demo-pool"]
    [request] = recorder.requests
    assert request.full_url.endswith("/opbeta/outposts/outpost_env-demo")
    assert request.get_method() == "DELETE"


def test_destroy_deletes_an_outpost_when_the_modal_app_is_not_deployed(tmp_path, monkeypatch):
    generated = initialize(tmp_path, monkeypatch)
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "super-secret-token")

    def lookup(name, *, create_if_missing):
        raise cli.modal.exception.NotFoundError(f"app {name} not found")

    monkeypatch.setattr(cli.modal, "App", SimpleNamespace(lookup=lookup))
    monkeypatch.setattr(
        cli,
        "_modal_stop",
        lambda name: (_ for _ in ()).throw(AssertionError("no deployed app")),
    )
    recorder = RecordingUrlOpen([{}])
    monkeypatch.setattr(cli, "OutpostsClient", _fake_outposts_client(recorder))

    cli.destroy(generated, yes=True)

    assert len(recorder.requests) == 1


def test_destroy_preserves_the_outpost_when_modal_stop_fails(tmp_path, monkeypatch):
    generated = initialize(tmp_path, monkeypatch)
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "super-secret-token")
    monkeypatch.setattr(cli.modal, "App", SimpleNamespace(lookup=Mock(return_value=None)))
    monkeypatch.setattr(
        cli,
        "_modal_stop",
        lambda name: SimpleNamespace(returncode=7, stdout="", stderr="offline"),
    )
    monkeypatch.setattr(
        cli,
        "OutpostsClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("outpost must be preserved")),
    )

    with pytest.raises(SystemExit, match=r"Destroy incomplete.*Modal stop"):
        cli.destroy(generated, yes=True)


def test_destroy_requires_yes_non_interactively_before_remote_changes(tmp_path, monkeypatch):
    generated = initialize(tmp_path, monkeypatch)
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "super-secret-token")
    monkeypatch.setattr(
        cli.modal,
        "App",
        SimpleNamespace(
            lookup=lambda *a, **k: (_ for _ in ()).throw(AssertionError("remote lookup"))
        ),
    )

    with pytest.raises(SystemExit, match="requires --yes"):
        cli.destroy(generated)


def test_destroy_rejects_unrecognizable_files_before_remote_changes(tmp_path, monkeypatch):
    custom = tmp_path / "custom.py"
    custom.write_text("print('not an outpost')")
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(
        cli.modal,
        "App",
        SimpleNamespace(
            lookup=lambda *a, **k: (_ for _ in ()).throw(AssertionError("remote lookup"))
        ),
    )

    with pytest.raises(SystemExit, match="no resources were changed"):
        cli.destroy(custom, yes=True)


def test_modal_stop_uses_the_project_environment(monkeypatch):
    modal_command = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli, "_modal", modal_command)

    cli._modal_stop("modal-devin-demo")

    modal_command.assert_called_once_with("app", "stop", "--yes", "modal-devin-demo")


def test_doctor_reports_failed_required_check(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})

    with pytest.raises(SystemExit) as exc_info:
        cli.doctor(outposts_dir=tmp_path)

    assert exc_info.value.code == 1


def test_doctor_success_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: True)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})

    cli.doctor(outposts_dir=tmp_path)


def test_expected_app_name_reads_the_generated_worker_from_env_call(tmp_path, monkeypatch):
    generated = initialize(tmp_path, monkeypatch)

    assert cli._expected_app_name(generated) == "modal-devin-demo-pool"


def test_worker_config_reads_destroy_identifiers_without_executing_the_file(tmp_path, monkeypatch):
    generated = initialize(tmp_path, monkeypatch, api_url="https://api.example.com")

    config = cli._worker_config(generated)

    assert config is not None
    assert config.outpost_id == "outpost_env-demo"
    assert config.api_url == "https://api.example.com"
    assert config.app_name == "modal-devin-demo-pool"


def test_expected_app_name_is_none_for_unrecognizable_files(tmp_path):
    unrecognizable = tmp_path / "custom.py"
    unrecognizable.write_text("print('not a generated outpost file')")

    assert cli._expected_app_name(unrecognizable) is None


def test_doctor_passes_when_every_outpost_has_a_deployed_app(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    initialize(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: True)

    monkeypatch.setattr(cli.modal, "App", SimpleNamespace(lookup=Mock(return_value=None)))

    cli.doctor(outposts_dir=tmp_path)


def test_doctor_fails_when_an_outpost_has_no_deployed_app(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    initialize(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: True)

    def lookup(name, *, create_if_missing):
        raise cli.modal.exception.NotFoundError(f"app {name} not found")

    monkeypatch.setattr(cli.modal, "App", SimpleNamespace(lookup=lookup))

    with pytest.raises(SystemExit) as exc_info:
        cli.doctor(outposts_dir=tmp_path)

    assert exc_info.value.code == 1


def test_doctor_warns_without_failing_on_an_unrecognizable_outpost_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: True)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    (tmp_path / "custom.py").write_text("print('not a generated outpost file')")
    monkeypatch.setattr(
        cli.modal,
        "App",
        SimpleNamespace(
            lookup=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be looked up"))
        ),
    )

    cli.doctor(outposts_dir=tmp_path)


def test_secret_listing_uses_the_sdk(monkeypatch):
    list_secrets = Mock(
        return_value=[
            SimpleNamespace(name="one"),
            SimpleNamespace(name="two"),
            SimpleNamespace(name=None),
        ]
    )
    monkeypatch.setattr(
        cli.modal,
        "Secret",
        SimpleNamespace(objects=SimpleNamespace(list=list_secrets)),
    )

    assert cli._existing_secret_names() == {"one", "two"}
    list_secrets.assert_called_once_with()

    list_secrets.side_effect = RuntimeError("offline")
    assert cli._existing_secret_names() is None


def _forbid_remote_outpost_calls(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("remote outpost API call")

    monkeypatch.setattr(cli.OutpostsClient, "create_outpost", boom)
    monkeypatch.setattr(cli.OutpostsClient, "delete_outpost", boom)


@pytest.mark.parametrize(
    ("outpost_id", "heading", "question"),
    [
        (
            "",
            "Let's create a new Devin Outpost",
            "What would you like to name your new outpost?",
        ),
        (
            "outpost_env-existing",
            "Let's connect your Devin Outpost to Modal",
            "What would you like to call this outpost?",
        ),
    ],
)
def test_interactive_init_frames_whether_the_outpost_is_new(
    tmp_path, monkeypatch, outpost_id, heading, question
):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: True)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    monkeypatch.setattr(cli, "_erase_last_line", lambda: None)
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "super-secret-token")
    monkeypatch.setattr(
        cli,
        "OutpostsClient",
        lambda *args, **kwargs: SimpleNamespace(create_outpost=lambda name: "outpost_env-new"),
    )
    ask = Mock(return_value="gpu-h200")
    confirm = Mock(return_value=False)
    printed = Mock()
    monkeypatch.setattr(cli, "_ask", ask)
    monkeypatch.setattr(cli, "_confirm", confirm)
    monkeypatch.setattr(cli._console, "print", printed)

    cli.init_worker(outpost_id=outpost_id, outposts_dir=tmp_path)

    ask.assert_called_once_with(question)
    output = "\n".join(str(item.args[0]) for item in printed.call_args_list if item.args)
    assert heading in output
    assert "selecting a machine in Devin" in output
    assert "gpu-h200 or production-vpc" in output
    confirm.assert_called_once_with("Deploy gpu-h200 to Modal now?", default=True)


def test_init_rejects_invalid_api_url_before_remote_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    _forbid_remote_outpost_calls(monkeypatch)

    with pytest.raises(SystemExit, match="api_url"):
        cli.init_worker(
            name="demo",
            api_url="file:///tmp/token",
            outposts_dir=tmp_path,
        )


def test_init_rejects_invalid_outpost_name_before_remote_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    _forbid_remote_outpost_calls(monkeypatch)

    with pytest.raises(SystemExit, match="lowercase letters"):
        cli.init_worker(name="Bad Name", outposts_dir=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_init_reprompts_for_an_invalid_outpost_name_interactively(tmp_path, monkeypatch):
    token = "super-secret-token"
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: True)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    monkeypatch.setattr(cli, "_erase_last_line", lambda: None)
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", token)

    recorder = RecordingUrlOpen([{"metadata": {"outpost_id": "outpost_env-demo"}}])
    monkeypatch.setattr(cli, "OutpostsClient", _fake_outposts_client(recorder))
    names = iter(["Bad Name", "good-name_1"])
    monkeypatch.setattr(cli, "_ask", lambda *args, **kwargs: next(names))
    monkeypatch.setattr(cli, "_confirm", lambda *args, **kwargs: False)

    cli.init_worker(outposts_dir=tmp_path, deploy=False)

    [request] = recorder.requests
    assert json.loads(request.data)["name"] == "good-name_1"


def test_init_skips_outpost_name_validation_when_connecting_an_existing_outpost(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})

    cli.init_worker(name="Demo Worker", outpost_id="outpost_env-demo", outposts_dir=tmp_path)

    assert (tmp_path / "demo_worker.py").exists()


def test_init_rejects_empty_secret_name_before_remote_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    _forbid_remote_outpost_calls(monkeypatch)

    with pytest.raises(SystemExit, match="secret_name"):
        cli.init_worker(
            name="demo",
            outpost_id="outpost_env-demo",
            secret_name=" ",
            outposts_dir=tmp_path,
        )


def test_init_refuses_to_overwrite_before_remote_changes(tmp_path, monkeypatch):
    (tmp_path / "demo.py").write_text("existing")
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    _forbid_remote_outpost_calls(monkeypatch)

    with pytest.raises(SystemExit, match="already exists"):
        cli.init_worker(name="demo", outposts_dir=tmp_path)


def test_init_requires_a_token_to_create_a_new_outpost(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.delenv("DEVIN_OUTPOSTS_TOKEN", raising=False)
    _forbid_remote_outpost_calls(monkeypatch)

    with pytest.raises(SystemExit, match="DEVIN_OUTPOSTS_TOKEN is required"):
        cli.init_worker(name="demo", outposts_dir=tmp_path)


def test_init_creates_the_outpost_via_a_direct_api_call(tmp_path, monkeypatch):
    token = "super-secret-token"
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", token)

    recorder = RecordingUrlOpen([{"metadata": {"outpost_id": "outpost_env-demo"}}])
    monkeypatch.setattr(cli, "OutpostsClient", _fake_outposts_client(recorder))

    cli.init_worker(name="demo", outposts_dir=tmp_path)

    [request] = recorder.requests
    assert request.full_url == "https://api.devin.ai/opbeta/outposts"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "name": "demo",
        "platform": "linux",
        "description": "",
    }
    assert request.get_header("Authorization") == f"Bearer {token}"

    generated = (tmp_path / "demo.py").read_text()
    assert "outpost_id='outpost_env-demo'" in generated


def test_init_reports_an_outpost_api_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "super-secret-token")

    error = urllib.error.HTTPError("url", 405, "Method Not Allowed", Message(), None)
    recorder = RecordingUrlOpen([error])
    monkeypatch.setattr(cli, "OutpostsClient", _fake_outposts_client(recorder))

    with pytest.raises(SystemExit, match="405"):
        cli.init_worker(name="demo", outposts_dir=tmp_path)


def test_failed_write_rolls_back_a_newly_created_outpost(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "super-secret-token")

    create_recorder = RecordingUrlOpen([{"metadata": {"outpost_id": "outpost_env-demo"}}])
    delete_recorder = RecordingUrlOpen([{}])
    recorders = iter([create_recorder, delete_recorder])
    monkeypatch.setattr(
        cli,
        "OutpostsClient",
        lambda base_url, token, *, timeout: OutpostsClient(
            base_url, token, timeout=timeout, urlopen=next(recorders)
        ),
    )
    monkeypatch.setattr(
        Path,
        "write_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(SystemExit, match="rolled back outpost outpost_env-demo"):
        cli.init_worker(name="demo", outposts_dir=tmp_path)

    [delete_request] = delete_recorder.requests
    assert delete_request.full_url == "https://api.devin.ai/opbeta/outposts/outpost_env-demo"
    assert delete_request.get_method() == "DELETE"


def test_cli_help_presents_the_project_level_workflow(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.app(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "init" in output
    assert "deploy" in output
    assert "destroy" in output
    assert "doctor" in output
    assert "│ outpost " not in output
