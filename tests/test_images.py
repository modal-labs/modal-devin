from __future__ import annotations

from unittest.mock import Mock

import modal
import pytest

from modal_devin.images import (
    _DEVIN_BIN,
    _DEVIN_CLI_CONTRACT_CHECK,
    _DEVIN_CLI_INSTALL,
    _LOGFIRE_REQUIREMENT,
    _controller_image,
    _finalize_worker_image,
    _worker_image,
)


def test_worker_image_remains_composable():
    image = _worker_image(install_chrome=False, install_ffmpeg=False)

    customized = image.run_commands("echo custom")

    assert isinstance(customized, modal.Image)
    assert "local files" not in repr(image)


def test_devin_installer_does_not_suppress_failures():
    assert "|| true" not in _DEVIN_CLI_INSTALL


def test_devin_installer_verifies_the_binary_instead_of_trusting_install_shs_exit_code():
    # install.sh's own last step runs `devin setup`, an interactive OAuth wizard that
    # always fails without a TTY (as in an image build) even when the binary installed
    # fine. The install command must still fail if the binary is genuinely missing.
    assert _DEVIN_CLI_INSTALL.endswith(f"; test -x {_DEVIN_BIN}")


def test_worker_image_checks_the_unpinned_cli_contract_during_build():
    assert "--session" in _DEVIN_CLI_CONTRACT_CHECK
    assert "--outpost" in _DEVIN_CLI_CONTRACT_CHECK
    assert "--acceptor-id" in _DEVIN_CLI_CONTRACT_CHECK
    assert "DEVIN_REMOTE_SESSION_TOKEN" in _DEVIN_CLI_CONTRACT_CHECK


def test_controller_image_is_small_and_contains_the_runtime_source():
    image = _controller_image()

    representation = repr(image)
    assert "local files" in representation
    assert "chromium" not in representation
    assert "ffmpeg" not in representation


def test_runtime_images_install_logfire(monkeypatch):
    controller = Mock()
    controller.uv_pip_install.return_value = controller
    controller.add_local_python_source.return_value = controller
    monkeypatch.setattr(modal.Image, "debian_slim", Mock(return_value=controller))

    assert _controller_image() is controller
    controller.uv_pip_install.assert_called_once_with(_LOGFIRE_REQUIREMENT)

    worker = Mock()
    instrumented_worker = Mock()
    worker.uv_pip_install.return_value = instrumented_worker
    instrumented_worker.add_local_python_source.return_value = instrumented_worker

    assert _finalize_worker_image(worker) is instrumented_worker
    worker.uv_pip_install.assert_called_once_with(_LOGFIRE_REQUIREMENT)


@pytest.mark.parametrize("install_chrome", [True, False])
@pytest.mark.parametrize("install_ffmpeg", [True, False])
def test_worker_image_supports_optional_tooling(install_chrome, install_ffmpeg):
    assert isinstance(
        _worker_image(install_chrome=install_chrome, install_ffmpeg=install_ffmpeg),
        modal.Image,
    )
