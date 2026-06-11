"""Tests for cue-daemon CLI flags."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from cue.daemon import _cmd_start


class TestDaemonStartNoWait:
    def test_no_wait_returns_without_health_check(self, tmp_path):
        socket_path = tmp_path / "daemon.sock"
        pid_path = tmp_path / "daemon.pid"

        with (
            patch("cue.daemon._is_running", return_value=None),
            patch("cue.daemon.subprocess.Popen") as popen,
            patch("cue.daemon._wait_for_socket") as wait,
        ):
            popen.return_value = MagicMock(poll=MagicMock(return_value=None))
            _cmd_start(socket_path, pid_path, "WARNING", no_wait=True)

        wait.assert_not_called()
        popen.assert_called_once()
