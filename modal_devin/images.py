"""Composable Modal image recipes for Devin workers."""

from __future__ import annotations

import modal

_DEVIN_BIN = "/root/.local/bin/devin"
# Intentionally left unpinned for now. Supply-chain reproducibility is tracked separately.
# install.sh's own last step unconditionally runs `devin setup`, an interactive OAuth
# wizard that always fails without a TTY (as in this build). Workers authenticate via
# DEVIN_OUTPOSTS_TOKEN instead, so that failure is harmless -- but it's still the exit
# code of the whole `curl | bash` pipeline. Chaining the executable check with `;` makes
# "the binary actually works" the real success signal instead, so a genuine install
# failure (bad download, missing binary, ...) still fails the build.
_DEVIN_CLI_INSTALL = f"curl -fsSL https://cli.devin.ai/install.sh | bash; test -x {_DEVIN_BIN}"
_DEVIN_CLI_CONTRACT_CHECK = (
    f"{_DEVIN_BIN} worker start --help > /tmp/devin-worker-help "
    "&& grep -q -- '--session' /tmp/devin-worker-help "
    "&& grep -q -- '--outpost' /tmp/devin-worker-help "
    "&& grep -q -- '--acceptor-id' /tmp/devin-worker-help "
    "&& grep -q 'DEVIN_REMOTE_SESSION_TOKEN' /tmp/devin-worker-help "
    "&& rm /tmp/devin-worker-help"
)

_CHROME_PATH = "/usr/bin/chromium"
_LOGFIRE_REQUIREMENT = "logfire>=4.2,<5"


def _worker_image(
    *,
    python_version: str = "3.12",
    install_ffmpeg: bool = True,
    install_chrome: bool = True,
) -> modal.Image:
    """Return a composable base image for a Devin worker.

    The modal-devin source mount is deliberately *not* added here. The public
    ``Worker.prepare_image()`` phase adds it after all user build steps, which keeps
    Modal's fluent image API valid during customization.
    """
    image = (
        modal.Image.debian_slim(python_version=python_version)
        .apt_install("git", "curl", "ca-certificates", "tar")
        .run_commands(_DEVIN_CLI_INSTALL)
        .run_commands(_DEVIN_CLI_CONTRACT_CHECK)
        .run_commands("mkdir -p /root/workspace")
    )
    if install_ffmpeg:
        image = image.apt_install("ffmpeg")
    if install_chrome:
        image = image.apt_install("chromium").env({"DEVIN_CHROME_PATH": _CHROME_PATH})
    return image


def _controller_image(*, python_version: str = "3.12") -> modal.Image:
    """Return a small image for scheduler and other control-plane functions."""
    return (
        modal.Image.debian_slim(python_version=python_version)
        .uv_pip_install(_LOGFIRE_REQUIREMENT)
        .add_local_python_source("modal_devin")
    )


def _finalize_worker_image(image: modal.Image) -> modal.Image:
    """Add modal-devin source as the final, startup-mounted image operation."""
    return image.uv_pip_install(_LOGFIRE_REQUIREMENT).add_local_python_source("modal_devin")
