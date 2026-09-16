"""The modal-devin project CLI.

``modal-devin init`` prompts for missing values on an interactive terminal and
requires explicit values in scripts and CI.
"""

import ast
import os
import re
import subprocess
import sys
from collections import deque
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Annotated, TextIO, TypedDict, Unpack, cast

import cyclopts
import modal
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.text import Text

from modal_devin import ConfigurationError, OutpostsAPIError, Worker
from modal_devin._client import OutpostsClient
from modal_devin._config import (
    DEFAULT_API_TIMEOUT_SECONDS,
    DEFAULT_API_URL,
    WorkerConfig,
    validate_outpost_name,
)

app = cyclopts.App(name="modal-devin")

TEMPLATE = files("modal_devin").joinpath("templates", "outpost.py.tmpl")
DEVIN_TOKEN_URL = "https://docs.devin.ai/api-reference/authentication"
_MODULE_STEM_RE = re.compile(r"[^0-9A-Za-z_]+")

# create-next-app-style prompts: "? Question › answer" instead of Rich's default "Question: ".
_PROMPT_SUFFIX = Text(" › ", style="dim")
_console = Console()


class _PromptKwargs(TypedDict, total=False):
    console: Console
    password: bool
    choices: list[str]
    case_sensitive: bool
    show_default: bool
    show_choices: bool
    default: object
    stream: TextIO


OutpostNameArg = Annotated[
    str,
    cyclopts.Parameter(help="Human-readable Devin Outposts outpost name.", show_default=False),
]
OutpostIdOption = Annotated[
    str,
    cyclopts.Parameter(
        help="Existing Devin Outposts outpost id. Omit to create a new outpost.",
        show_default=False,
    ),
]
OutpostFileArg = Annotated[
    Path,
    cyclopts.Parameter(
        help="Generated Modal outpost file. Omit to use every outpost file in --outposts-dir.",
        show_default=False,
    ),
]
ApiUrlOption = Annotated[
    str,
    cyclopts.Parameter(help="Base URL for the Devin API."),
]
SecretNameOption = Annotated[
    str,
    cyclopts.Parameter(help="Modal Secret name containing DEVIN_OUTPOSTS_TOKEN."),
]
OutpostsDirOption = Annotated[
    Path,
    cyclopts.Parameter(help="Directory of generated Modal outpost files."),
]
DeployOption = Annotated[
    bool | None,
    cyclopts.Parameter(
        help="Deploy the generated outpost file after writing it.",
        show_default=False,
    ),
]
YesOption = Annotated[
    bool,
    cyclopts.Parameter(
        name=("--yes", "-y"),
        negative=False,
        help="Skip the destructive-action confirmation.",
    ),
]


class _WizardPrompt(Prompt):
    prompt_suffix = _PROMPT_SUFFIX


class _WizardConfirm(Confirm):
    prompt_suffix = _PROMPT_SUFFIX


def _erase_last_line() -> None:
    if not _interactive():
        return
    _console.file.write("\x1b[1A\x1b[2K")


def _mark_answered(question: str, shown_value: str) -> None:
    """Rewrite the just-answered prompt line in place, swapping the `?` for a `✔` -- only called
    from _ask/_confirm, which are only reached when stdout is already known to be a real tty."""
    _erase_last_line()
    _console.print(f"[bold green]✔[/bold green] [bold]{question}[/bold] [dim]›[/dim] {shown_value}")


def _ask(question: str, **kwargs: Unpack[_PromptKwargs]) -> str:
    answer = cast(
        str,
        _WizardPrompt.ask(f"[bold green]?[/bold green] [bold]{question}[/bold]", **kwargs),
    ).strip()
    if kwargs.get("password"):
        # Fixed-width mask, not "*" * len(answer): a real token is long enough that echoing its
        # length back as a wall of asterisks is both ugly and a pointless bit of a leak.
        shown = "•" * 8 if answer else "[dim]skipped[/dim]"
    else:
        shown = answer or "[dim]skipped[/dim]"
    _mark_answered(question, shown)
    return answer


def _confirm(question: str, **kwargs: Unpack[_PromptKwargs]) -> bool:
    answer = bool(
        _WizardConfirm.ask(f"[bold green]?[/bold green] [bold]{question}[/bold]", **kwargs)
    )
    _mark_answered(question, "yes" if answer else "no")
    return answer


def _step_start(running: str) -> None:
    """Print a dim in-progress line; pair with _step_done/_step_failed to redraw it as a
    checkmark or cross once the step resolves -- create-next-app style."""
    if _interactive():
        _console.print(f"[dim]{running}[/dim]")
    else:
        _console.print(running)


def _step_done(text: str) -> None:
    if _interactive():
        _erase_last_line()
    _console.print(f"[bold green]✔[/bold green] {text}")


def _step_failed(text: str) -> None:
    if _interactive():
        _erase_last_line()
    _console.print(f"[bold red]✖[/bold red] {text}")


def _done(text: str) -> None:
    """A completed-step line for actions with no visible delay (no matching _step_start), so
    nothing needs erasing first."""
    _console.print(f"[bold green]✔[/bold green] {text}")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _modal(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `modal` via the current interpreter rather than a bare `modal` on PATH: `modal` is
    a declared dependency, so this consistently uses the project's selected environment."""
    return subprocess.run(
        [sys.executable, "-m", "modal", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_with_tail(argv: Sequence[str], window: int = 10) -> int:
    """Run a subprocess, showing only the last `window` lines of its output at a time -- enough
    to see it's alive and what it's doing, without flooding the terminal with a full build log.
    The final `window` lines stick around afterward (e.g. modal deploy's own success/URL line)."""
    lines: deque[str] = deque(maxlen=window)
    all_lines: list[str] = []
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    if proc.stdout is None:  # pragma: no cover - guaranteed by stdout=PIPE
        raise RuntimeError("failed to capture subprocess output")
    try:
        with Live(console=_console, refresh_per_second=12, transient=True) as live:
            for line in proc.stdout:
                rendered = line.rstrip()
                lines.append(rendered)
                all_lines.append(rendered)
                live.update(Text("\n".join(lines), style="dim"))
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    visible_lines = all_lines if proc.returncode else list(lines)
    if visible_lines:
        _console.print(Text("\n".join(visible_lines), style="dim"))
    return proc.returncode


def _modal_deploy(outpost_file: Path) -> int:
    # Session invocations are long-lived: a rolling deployment lets the old
    # Function version finish serving every input it already owns while the new
    # scheduler version begins dispatching work.  Spell the strategy out instead
    # of inheriting the Modal CLI default so a dependency upgrade or local CLI
    # configuration cannot silently turn a code deployment into a disruptive
    # recreate deployment.
    argv = [
        sys.executable,
        "-m",
        "modal",
        "deploy",
        "--strategy",
        "rolling",
        str(outpost_file),
    ]
    if _interactive():
        return _run_with_tail(argv)
    return subprocess.run(argv).returncode


def _existing_secret_names() -> set[str] | None:
    """Names of secrets already in the workspace, or None if the lookup failed."""
    try:
        secrets = modal.Secret.objects.list()
    except Exception:
        return None
    return {secret.name for secret in secrets if secret.name is not None}


def _create_modal_secret(secret_name: str, token: str) -> None:
    """Create a named Modal Secret directly, keeping the token in memory."""
    modal.Secret.objects.create(
        secret_name,
        {"DEVIN_OUTPOSTS_TOKEN": token},
    )


def _delete_outpost(api_url: str, token: str | None, outpost_id: str) -> bool:
    """Best-effort rollback of an outpost `create` just registered with Devin, for when a later
    (local, avoidable) step fails before the outpost file is on disk -- otherwise the command exits
    having created a live outpost with no local record of it. Returns whether the delete succeeded;
    callers should print manual cleanup instructions on False."""
    if not token:
        return False
    client = OutpostsClient(api_url, token, timeout=DEFAULT_API_TIMEOUT_SECONDS)
    try:
        client.delete_outpost(outpost_id)
    except OutpostsAPIError:
        return False
    return True


def _modal_is_configured() -> bool:
    """Whether Modal accepts the token available to the current Python environment."""
    try:
        return _modal("token", "info").returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _module_stem(name: str) -> str:
    stem = _MODULE_STEM_RE.sub("_", name).strip("_").lower()
    if not stem:
        raise SystemExit("NAME must contain at least one ASCII letter or digit")
    if stem[0].isdigit():
        stem = f"outpost_{stem}"
    return stem


def _python_literal(value: str) -> str:
    return repr(value)


def _worker_config(file: Path) -> WorkerConfig | None:
    """The worker identity a generated outpost file declares, without executing it.

    Returns None for files whose `Worker.from_env(...)` call or required arguments
    aren't statically recognizable -- e.g. heavily hand-edited files -- rather than
    running arbitrary local code to inspect them.
    """
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "from_env"
            and node.args
        ):
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        outpost_id_node = keywords.get("outpost_id")
        if outpost_id_node is None:
            return None
        api_url_node = keywords.get("api_url")
        try:
            name = ast.literal_eval(node.args[0])
            outpost_id = ast.literal_eval(outpost_id_node)
            api_url = DEFAULT_API_URL if api_url_node is None else ast.literal_eval(api_url_node)
        except (TypeError, ValueError):
            return None
        if not all(isinstance(value, str) for value in (name, outpost_id, api_url)):
            return None
        try:
            return WorkerConfig(name=name, outpost_id=outpost_id, api_url=api_url)
        except ConfigurationError:
            return None
    return None


def _expected_app_name(file: Path) -> str | None:
    config = _worker_config(file)
    return None if config is None else config.app_name


def _modal_stop(app_name: str) -> subprocess.CompletedProcess[str]:
    return _modal("app", "stop", "--yes", app_name)


@app.command
def deploy(
    outpost_file: OutpostFileArg | None = None,
    *,
    outposts_dir: OutpostsDirOption = Path("outposts"),
) -> None:
    """Deploy a generated Modal application using modal-devin's Python environment."""
    if outpost_file is not None:
        returncode = _modal_deploy(outpost_file)
        if returncode != 0:
            raise SystemExit(returncode)
        return

    outpost_files = sorted(outposts_dir.glob("*.py"))
    if not outpost_files:
        raise SystemExit(f"No outpost files found in {outposts_dir}")

    failed: list[Path] = []
    for file in outpost_files:
        _console.print(f"[dim]Running modal deploy {file}...[/dim]")
        returncode = _modal_deploy(file)
        if returncode == 0:
            _console.print(f"[bold green]✔[/bold green] Deployed [bold]{escape(str(file))}[/bold]")
        else:
            _console.print(
                f"[bold red]✖[/bold red] {escape(str(file))} failed (exit code {returncode})"
            )
            failed.append(file)

    if failed:
        joined = ", ".join(str(file) for file in failed)
        raise SystemExit(
            f"{len(failed)} of {len(outpost_files)} outpost(s) failed to deploy: {joined}"
        )


@app.command
def destroy(
    outpost_file: OutpostFileArg | None = None,
    *,
    outposts_dir: OutpostsDirOption = Path("outposts"),
    yes: YesOption = False,
) -> None:
    """Stop Modal applications and delete their Devin outposts."""
    outpost_files = (
        [outpost_file] if outpost_file is not None else sorted(outposts_dir.glob("*.py"))
    )
    if not outpost_files:
        raise SystemExit(f"No outpost files found in {outposts_dir}")

    targets: list[tuple[Path, WorkerConfig]] = []
    for file in outpost_files:
        config = _worker_config(file)
        if config is None:
            raise SystemExit(
                f"Could not determine the outpost ID and Modal app name from {file}; "
                "no resources were changed"
            )
        targets.append((file, config))

    if not yes:
        if not _interactive():
            raise SystemExit("destroy requires --yes when run non-interactively")
        count = len(targets)
        if not _confirm(
            f"Destroy {count} outpost{'s' if count != 1 else ''}? "
            "This immediately stops Modal containers and permanently deletes the Devin "
            f"outpost{'s' if count != 1 else ''}.",
            default=False,
        ):
            _console.print("Destroy cancelled")
            return

    token = os.environ.get("DEVIN_OUTPOSTS_TOKEN")
    if not token and _interactive():
        token = _ask("Admin-scoped Devin Enterprise service user key", password=True)
    if not token:
        raise SystemExit(
            "DEVIN_OUTPOSTS_TOKEN is required to delete Devin outposts "
            "(set the env var, or provide it when prompted)"
        )

    failures: list[str] = []
    for file, config in targets:
        modal_ready = False
        try:
            modal.App.lookup(config.app_name, create_if_missing=False)
        except modal.exception.NotFoundError:
            _done(f"Modal app [bold]{escape(config.app_name)}[/bold] is not deployed")
            modal_ready = True
        except Exception as error:
            _step_failed(
                f"Could not inspect Modal app {escape(config.app_name)}: {escape(str(error))}"
            )
            failures.append(f"{file} (Modal lookup)")
        else:
            _step_start(f"Stopping Modal app {config.app_name}...")
            try:
                result = _modal_stop(config.app_name)
            except (OSError, subprocess.TimeoutExpired) as error:
                _step_failed(
                    f"Modal app {escape(config.app_name)} stop failed: {escape(str(error))}"
                )
                failures.append(f"{file} (Modal stop)")
            else:
                if result.returncode == 0:
                    _step_done(f"Stopped Modal app [bold]{escape(config.app_name)}[/bold]")
                    modal_ready = True
                else:
                    detail = (result.stderr or result.stdout).strip()
                    suffix = (
                        f": {escape(detail)}" if detail else f" (exit code {result.returncode})"
                    )
                    _step_failed(f"Modal app {escape(config.app_name)} stop failed{suffix}")
                    failures.append(f"{file} (Modal stop)")

        # Keep the Devin outpost intact if its worker may still be running. This makes a
        # Modal credential or network failure retryable instead of leaving a live scheduler
        # pointed at an outpost that has already disappeared.
        if not modal_ready:
            continue

        _step_start(f"Deleting Devin outpost {config.outpost_id}...")
        client = OutpostsClient(
            config.api_url,
            token,
            timeout=DEFAULT_API_TIMEOUT_SECONDS,
        )
        try:
            client.delete_outpost(config.outpost_id)
        except OutpostsAPIError as error:
            _step_failed(
                f"Devin outpost {escape(config.outpost_id)} delete failed: {escape(str(error))}"
            )
            failures.append(f"{file} (Devin outpost delete)")
        else:
            _step_done(f"Deleted Devin outpost [bold]{escape(config.outpost_id)}[/bold]")

    if failures:
        joined = ", ".join(failures)
        raise SystemExit(f"Destroy incomplete; failed: {joined}")


@app.command(name="init")
def init_worker(
    name: OutpostNameArg = "",
    *,
    outpost_id: OutpostIdOption = "",
    api_url: ApiUrlOption = DEFAULT_API_URL,
    secret_name: SecretNameOption = "devin-outposts-token",
    outposts_dir: OutpostsDirOption = Path("outposts"),
    deploy: DeployOption = None,
) -> None:
    """Scaffold a Modal application for Devin Outposts."""
    interactive = _interactive()

    if interactive:
        _console.print()
        if outpost_id:
            _console.print("[bold]Let's connect your Devin Outpost to Modal[/bold]")
            _console.print(
                "[dim]We'll generate its Modal app, connect the credentials, and help you "
                "deploy it.[/dim]"
            )
        else:
            _console.print("[bold]Let's create a new Devin Outpost[/bold]")
            _console.print(
                "[dim]We'll choose a name, connect Devin to Modal, and help you deploy it.[/dim]"
            )
        _console.print()

    if interactive and not _modal_is_configured() and _confirm("Set up Modal now?", default=True):
        subprocess.run([sys.executable, "-m", "modal", "setup"])
        if _modal_is_configured():
            _done("Modal is set up")

    if interactive and not name:
        _console.print(
            "[dim]Choose a name people will recognize when selecting a machine in Devin, "
            "like gpu-h200 or production-vpc (lowercase letters, digits, dashes, and underscores)."
            "[/dim]"
        )
    while True:
        while not name:
            if not interactive:
                raise SystemExit("NAME is required (pass it as an argument, or run interactively)")
            question = (
                "What would you like to call this outpost?"
                if outpost_id
                else "What would you like to name your new outpost?"
            )
            name = _ask(question)
        if outpost_id:
            break
        try:
            validate_outpost_name(name)
        except ConfigurationError as error:
            if not interactive:
                raise SystemExit(str(error)) from error
            _console.print(f"[red]{escape(str(error))}[/red]")
            name = ""
            continue
        break

    if not secret_name.strip():
        raise SystemExit("secret_name must not be empty")

    try:
        validated_worker = Worker(
            name,
            outpost_id=outpost_id or "pending",
            api_url=api_url,
        )
    except ConfigurationError as error:
        raise SystemExit(str(error)) from error

    # Resolve and validate the output path *before* creating anything remotely, so the one
    # foreseeable, locally-checkable failure (name collision) never even reaches the API.
    # Anything that fails after that point (mkdir, template write, ...) rolls the outpost back via
    # DELETE /opbeta/outposts/{outpost_id} instead, so `create` doesn't leave a live outpost behind
    # with no local file to show for it.
    out_dir = outposts_dir
    out_path = out_dir / f"{_module_stem(name)}.py"
    if out_path.exists():
        raise SystemExit(f"{out_path} already exists, not overwriting")

    # The admin-scoped Devin Enterprise service user key, if we get one.
    token = None
    # Set once we register a *new* outpost; rolled back if a later step fails.
    created_outpost_id = None

    if not outpost_id:
        token = os.environ.get("DEVIN_OUTPOSTS_TOKEN")
        if not token and interactive:
            _console.print(
                "[dim]To create the outpost, paste a key from an admin-scoped Devin Enterprise "
                f"service user. Create one at {DEVIN_TOKEN_URL}[/dim]"
            )
            token = _ask("Paste your Devin service user key", password=True)
        if not token:
            raise SystemExit(
                "DEVIN_OUTPOSTS_TOKEN is required to create a new outpost and must contain "
                "an admin-scoped Enterprise service user key "
                "(set the env var, or provide it when prompted)"
            )

        _step_start(f"Creating outpost {name}...")
        client = OutpostsClient(api_url, token, timeout=DEFAULT_API_TIMEOUT_SECONDS)
        try:
            outpost_id = client.create_outpost(name)
        except OutpostsAPIError as error:
            _step_failed(f"outpost create {name} failed")
            raise SystemExit(str(error)) from error
        created_outpost_id = outpost_id
        _step_done(
            f"Created outpost [bold]{escape(name)}[/bold] [dim]→[/dim] "
            f"[cyan]{escape(outpost_id)}[/cyan]"
        )

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        generated = Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
            app_name=_python_literal(validated_worker.app_name),
            outpost_name=_python_literal(name),
            outpost_id=_python_literal(outpost_id),
            api_url=_python_literal(api_url),
            secret_name=_python_literal(secret_name),
        )
        out_path.write_text(generated, encoding="utf-8")
    except OSError as e:
        if created_outpost_id and _delete_outpost(api_url, token, created_outpost_id):
            raise SystemExit(
                f"Failed to write {out_path}, rolled back outpost {created_outpost_id}: {e}"
            ) from e
        elif created_outpost_id:
            raise SystemExit(
                f"Failed to write {out_path}: {e}\n"
                f"Also failed to roll back outpost {name} ({created_outpost_id}) -- "
                f"delete it from the Devin dashboard or with the Outposts API."
            ) from e
        raise SystemExit(f"Failed to write {out_path}: {e}") from e
    _done(f"Wrote [cyan]{escape(str(out_path))}[/cyan]")

    existing_secrets = _existing_secret_names()
    secret_created = False
    if existing_secrets is not None and secret_name in existing_secrets:
        _done(f"Modal secret [bold]{escape(secret_name)}[/bold] already exists")
    elif interactive:
        if not token:
            _console.print(
                "[dim]To run the outpost, Modal needs a key from an admin-scoped Devin "
                f"Enterprise service user. Create one at {DEVIN_TOKEN_URL}[/dim]"
            )
            token = _ask(
                "Paste your Devin service user key [dim](leave blank to skip)[/dim]",
                password=True,
                default="",
                show_default=False,
            )
        if token and _confirm(f"Save this key in Modal as {secret_name}?", default=True):
            _step_start(f"Creating Modal secret {secret_name}...")
            try:
                _create_modal_secret(secret_name, token)
            except Exception as error:
                _step_failed(f"Modal secret {secret_name} creation failed")
                _console.print(str(error))
            else:
                _step_done(f"Created Modal secret [bold]{secret_name}[/bold]")
                secret_created = True

    if not secret_created and secret_name not in (existing_secrets or set()):
        _console.print(
            f"[dim]next:[/dim] create Modal secret [bold]{escape(secret_name)}[/bold] "
            "with a [bold]DEVIN_OUTPOSTS_TOKEN[/bold] value"
        )

    should_deploy = (
        deploy
        if deploy is not None
        else interactive
        and _confirm(
            f"Deploy {name} to Modal now?",
            default=True,
        )
    )
    if should_deploy:
        _console.print(f"[dim]Running modal deploy {out_path}...[/dim]")
        returncode = _modal_deploy(out_path)
        if returncode == 0:
            _done(f"Deployed [bold]{name}[/bold]")
            _console.print()
            _console.print(f"[bold green]Your outpost {escape(name)} is ready.[/bold green]")
        else:
            _console.print(f"[bold red]✖[/bold red] modal deploy failed (exit code {returncode})")
            raise SystemExit(returncode)
    else:
        _console.print("[dim]When you're ready, deploy with:[/dim]")
        _console.print(f"  modal-devin deploy [cyan]{escape(str(out_path))}[/cyan]")


@app.command
def doctor(
    *,
    secret_name: SecretNameOption = "devin-outposts-token",
    outposts_dir: OutpostsDirOption = Path("outposts"),
) -> None:
    """Check local Modal and worker-runtime prerequisites."""
    failures = 0

    if _modal_is_configured():
        _done("Modal credentials are valid")
    else:
        _step_failed("Modal credentials are missing or invalid; run `modal setup`")
        failures += 1

    secrets = _existing_secret_names()
    if secrets is None:
        _step_failed("Could not inspect Modal secrets")
        failures += 1
    elif secret_name in secrets:
        _done(f"Modal secret [bold]{escape(secret_name)}[/bold] exists")
    else:
        _step_failed(f"Modal secret {escape(secret_name)} does not exist")
        failures += 1

    for outpost_file in sorted(outposts_dir.glob("*.py")):
        app_name = _expected_app_name(outpost_file)
        if app_name is None:
            _console.print(
                f"[yellow]![/yellow] Could not determine the Modal app for "
                f"{escape(str(outpost_file))}"
            )
            continue
        try:
            modal.App.lookup(app_name, create_if_missing=False)
        except modal.exception.NotFoundError:
            _step_failed(f"{escape(str(outpost_file))} has no deployed Modal app ({app_name})")
            failures += 1
        else:
            _done(f"{escape(str(outpost_file))} is deployed as [bold]{escape(app_name)}[/bold]")

    if failures:
        raise SystemExit(1)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
