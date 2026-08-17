"""Things the accelerator depends on go away, and it survives them.

Covers the library disappearing off a network mount, and the ML engine wedging
and being replaced without silently downgrading to the slow one.

macOS drops SMB and NFS mounts on sleep, on a flaky network, and when a NAS
reboots. Immich stores absolute paths, so every job then fails instantly on
ENOENT: nothing crashes, the queue drains into failures, and the logs never
name the cause. These cover noticing it, pausing, putting the mount back, and
starting again.
"""

import argparse
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import immich_accelerator.__main__ as m

MOUNT_OUTPUT = """/dev/disk2s4s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
//eric@nas/photos on /Volumes/photos (smbfs, nodev, nosuid)
//eric@nas/Time%20Machine on /Volumes/Time Machine (smbfs, nobrowse)
nas:/volume1/media on /nas (nfs, nodev)
/dev/disk1s2 on /Volumes/Fast Storage (hfs, local, journaled)
"""


def _mount(output=MOUNT_OUTPUT):
    return patch.object(
        m.subprocess, "run", return_value=MagicMock(stdout=output, returncode=0)
    )


def drive_watch(cfg, ready, pids=None, recipe=None, seconds_per_cycle=60.0):
    """Run the real watch loop over a scripted sequence of probe results.

    The clock advances a cycle's worth on every probe, so tests can express
    "unreachable for two minutes" rather than counting monotonic() calls, which
    the loop makes from several places.
    """
    pids = dict(pids or {})
    clock = {"t": 1000.0}
    results = list(ready)

    def probe(_config):
        clock["t"] += seconds_per_cycle
        return results.pop(0) if results else True

    mocks = {
        "load_config": MagicMock(
            side_effect=[dict(cfg)] * (len(ready) + 1) + [KeyboardInterrupt()]
        ),
        "media_ready_now": MagicMock(side_effect=probe),
        "read_pid": MagicMock(side_effect=lambda n: pids.get(n)),
        "kill_pid": MagicMock(side_effect=lambda n, **kw: pids.pop(n, None)),
        "cmd_start": MagicMock(),
        "cmd_stop": MagicMock(),
        "reconcile_components": MagicMock(),
        "cap_service_logs": MagicMock(),
        "_upgraded_on_disk": MagicMock(return_value=False),
        "_worker_fd_total": MagicMock(return_value=None),
        "mount_recipe_for": MagicMock(return_value=recipe),
        "save_config": MagicMock(),
        "_attempt_remount": MagicMock(),
        "diagnose_worker_log": MagicMock(return_value=None),
        "log": MagicMock(),
    }
    with patch.multiple(m, **mocks), patch.object(
        m.time, "monotonic", side_effect=lambda: clock["t"]
    ):
        m._watch_worker(dict(cfg))
    return mocks


class TestMountRecipe:
    """What we record while the mount is healthy, since it cannot be read back
    once the mount is gone."""

    def test_finds_the_smb_share_under_the_library(self):
        with _mount():
            r = m.mount_recipe_for("/Volumes/photos/library")
        assert r == {
            "fstype": "smbfs",
            "spec": "//eric@nas/photos",
            "mountpoint": "/Volumes/photos",
        }

    def test_finds_an_nfs_mount_at_a_custom_path(self):
        """The whole point of replaying the recipe: NAS users mount at /nas,
        not at /Volumes/<share>, and Immich's database says /nas."""
        with _mount():
            r = m.mount_recipe_for("/nas/photos/upload")
        assert r["fstype"] == "nfs"
        assert r["mountpoint"] == "/nas"

    def test_mount_points_containing_spaces_parse(self):
        """Split on the first ' on ', not the last: '/Volumes/Time Machine'
        contains no ' on ' but plenty of paths do, and a mis-split silently
        records a mount point that does not exist."""
        with _mount():
            r = m.mount_recipe_for("/Volumes/Time Machine/x")
        assert r["mountpoint"] == "/Volumes/Time Machine"
        assert r["spec"] == "//eric@nas/Time%20Machine"

    def test_local_disks_are_never_recorded(self):
        """Remounting an APFS volume is not our business, and pretending we
        could would turn a dead disk into a retry loop."""
        with _mount():
            assert m.mount_recipe_for("/Volumes/Fast Storage/photos") is None
            assert m.mount_recipe_for("/var/lib/photos") is None

    def test_the_longest_matching_mount_wins(self):
        nested = MOUNT_OUTPUT + "//eric@nas/inner on /nas/inner (smbfs, nodev)\n"
        with _mount(nested):
            assert m.mount_recipe_for("/nas/inner/photos")["mountpoint"] == "/nas/inner"

    def test_a_path_reached_through_a_symlink_still_matches(self, tmp_path):
        """/sbin/mount reports resolved paths. On a real Mac /tmp is a symlink
        to /private/tmp, and an unresolved compare silently finds no mount at
        all, which reads as "nothing to remount" rather than as a bug."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        out = f"nas:/vol on {real} (nfs, nodev)\n"
        with _mount(out):
            r = m.mount_recipe_for(str(link / "photos"))
        assert r is not None and r["mountpoint"] == str(real)

    def test_survives_mount_being_unavailable(self):
        with patch.object(m.subprocess, "run", side_effect=OSError("boom")):
            assert m.mount_recipe_for("/nas") is None


class TestRemount:
    RECIPE = {"fstype": "smbfs", "spec": "//eric@nas/photos", "mountpoint": "/nas"}

    def _run(self, returncode=0, stderr=""):
        return patch.object(
            m.subprocess,
            "run",
            return_value=MagicMock(returncode=returncode, stderr=stderr, stdout=""),
        )

    def test_smb_mounts_without_ever_prompting(self, tmp_path):
        """No terminal is attached to a launchd watcher. Without -N,
        mount_smbfs waits for a password that nobody can type and the watch
        loop is wedged for good."""
        recipe = dict(self.RECIPE, mountpoint=str(tmp_path / "nas"))
        with self._run() as run:
            ok, _ = m.remount(recipe)
        assert ok
        assert run.call_args.args[0][:2] == ["/sbin/mount_smbfs", "-N"]

    def test_the_mount_point_is_created_if_it_vanished(self, tmp_path):
        point = tmp_path / "nas" / "photos"
        with self._run():
            m.remount(dict(self.RECIPE, mountpoint=str(point)))
        assert point.is_dir()

    def test_a_rejected_password_is_reported_as_auth(self, tmp_path):
        recipe = dict(self.RECIPE, mountpoint=str(tmp_path / "nas"))
        with self._run(
            1, "mount_smbfs: server rejected the connection: " "Authentication error"
        ):
            ok, reason = m.remount(recipe)
        assert not ok and reason == "auth"

    def test_a_network_failure_is_not_auth(self, tmp_path):
        recipe = dict(self.RECIPE, mountpoint=str(tmp_path / "nas"))
        with self._run(1, "mount_smbfs: server connection failed: " "No route to host"):
            ok, reason = m.remount(recipe)
        assert not ok and reason != "auth"

    def test_a_hung_server_does_not_hang_the_watcher(self, tmp_path):
        recipe = dict(self.RECIPE, mountpoint=str(tmp_path / "nas"))
        with patch.object(
            m.subprocess, "run", side_effect=subprocess.TimeoutExpired("mount", 60)
        ):
            ok, reason = m.remount(recipe)
        assert not ok and "timed out" in reason


class TestRemountBackoff:
    """Retrying is fine. Retrying a password we already know is wrong, every
    thirty seconds, forever, is how an account gets locked."""

    CFG = {
        "mount_recipe": {
            "fstype": "smbfs",
            "spec": "//eric@nas/photos",
            "mountpoint": "/nas",
        }
    }

    def test_a_rejected_password_stops_all_further_attempts(self):
        state: dict = {}
        with patch.object(
            m, "remount", return_value=(False, "auth")
        ) as r, patch.object(m, "log"):
            for _ in range(20):
                m._attempt_remount(dict(self.CFG), state)
        assert r.call_count == 1, "must never retry credentials the server refused"
        assert state["blocked"]

    def test_a_network_failure_backs_off_instead_of_hammering(self):
        """The first retry is immediate, the next is a minute later. Without
        the wait a NAS that is off overnight gets 2880 mount attempts."""
        state: dict = {}
        clock = [1000.0]
        with patch.object(
            m, "remount", return_value=(False, "down")
        ) as r, patch.object(
            m.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(
            m, "log"
        ):
            m._attempt_remount(dict(self.CFG), state)
            assert r.call_count == 1
            clock[0] += 30  # next watch cycle
            m._attempt_remount(dict(self.CFG), state)
            assert r.call_count == 1, "30s in, still inside the 60s backoff"
            clock[0] += 40
            m._attempt_remount(dict(self.CFG), state)
            assert r.call_count == 2

    def test_nothing_is_attempted_without_a_recorded_recipe(self):
        with patch.object(m, "remount") as r, patch.object(m, "log"):
            m._attempt_remount({}, {})
        r.assert_not_called()

    def test_success_clears_the_backoff(self):
        """The clock is pinned: monotonic() counts from boot, so leaving it real
        makes this pass on a long-running Mac and fail on a fresh CI container,
        where not enough time has elapsed to clear the backoff window."""
        state = {"attempts": 3, "last": 1.0}
        with patch.object(m, "remount", return_value=(True, "")), patch.object(
            m.time, "monotonic", return_value=100_000.0
        ), patch.object(m, "log"):
            m._attempt_remount(dict(self.CFG), state)
        assert state == {}


@pytest.fixture(autouse=True)
def _no_real_waiting():
    """The loop sleeps 30s a cycle and installs a SIGTERM handler; neither is
    what these tests are about."""
    with patch("time.sleep"), patch("signal.signal"):
        yield


class TestWatchLoopPausesOnMissingLibrary:
    """The behaviour that matters: the running service reacts."""

    CFG = {
        "worker": True,
        "ml": False,
        "upload_mount": "/nas/photos",
        "media_id": "abc123",
        "mount_recipe": {
            "fstype": "nfs",
            "spec": "nas:/volume1/media",
            "mountpoint": "/nas",
        },
    }

    def _drive(self, cfg, ready, **kw):
        return drive_watch(cfg, ready, **kw)

    def test_the_worker_is_stopped_when_the_library_disappears(self, tmp_data_dir):
        mocks = self._drive(self.CFG, [False, False, False], pids={"worker": 4242})
        assert "worker" in [c.args[0] for c in mocks["kill_pid"].call_args_list]

    def test_the_worker_is_not_restarted_into_a_missing_library(self, tmp_data_dir):
        """We kill the worker, then the crash-check further down the same cycle
        sees no pid and would restart it. If the pause does not skip the rest of
        the cycle the worker is back within seconds and every job still fails,
        so this models the kill actually taking effect.
        """
        live = {"worker": 4242}
        mocks = {
            "load_config": MagicMock(
                side_effect=[dict(self.CFG), dict(self.CFG), KeyboardInterrupt()]
            ),
            "media_ready_now": MagicMock(return_value=False),
            "read_pid": MagicMock(side_effect=lambda n: live.get(n)),
            "kill_pid": MagicMock(side_effect=lambda n, **kw: live.pop(n, None)),
            "cmd_start": MagicMock(),
            "cmd_stop": MagicMock(),
            "reconcile_components": MagicMock(),
            "cap_service_logs": MagicMock(),
            "_upgraded_on_disk": MagicMock(return_value=False),
            "_worker_fd_total": MagicMock(return_value=None),
            "mount_recipe_for": MagicMock(return_value=None),
            "save_config": MagicMock(),
            "_attempt_remount": MagicMock(),
            "diagnose_worker_log": MagicMock(return_value=None),
            "log": MagicMock(),
        }
        with patch.multiple(m, **mocks):
            m._watch_worker(dict(self.CFG))
        mocks["cmd_start"].assert_not_called()

    def test_a_remount_is_attempted_while_it_is_down(self, tmp_data_dir):
        mocks = self._drive(self.CFG, [False])
        mocks["_attempt_remount"].assert_called_once()

    def test_the_worker_starts_again_when_the_library_returns(self, tmp_data_dir):
        """Gone, then back. Nobody should have to notice this happened, which
        is the entire point of a service that runs unattended."""
        mocks = self._drive(self.CFG, [False, True])
        assert mocks["cmd_start"].called, "worker must come back by itself"

    def test_the_recipe_is_recorded_while_the_mount_is_healthy(self, tmp_data_dir):
        """Recorded now or never: /sbin/mount cannot describe a mount that is
        already gone."""
        recipe = {"fstype": "nfs", "spec": "nas:/x", "mountpoint": "/nas"}
        cfg = {k: v for k, v in self.CFG.items() if k != "mount_recipe"}
        mocks = self._drive(cfg, [True], pids={"worker": 999}, recipe=recipe)
        assert mocks["save_config"].called, "a recipe we never saved is no recipe"
        assert mocks["save_config"].call_args.args[0]["mount_recipe"] == recipe

    def test_an_unchanged_recipe_is_not_rewritten_every_cycle(self, tmp_data_dir):
        """config.json would otherwise be rewritten twice a minute forever."""
        recipe = self.CFG["mount_recipe"]
        mocks = self._drive(self.CFG, [True, True], pids={"worker": 999}, recipe=recipe)
        mocks["save_config"].assert_not_called()

    def test_an_install_without_a_media_marker_is_left_alone(self, tmp_data_dir):
        """A local library, or one predating the marker, must not be probed
        into a pause it can never leave."""
        assert m.media_ready_now({"upload_mount": "", "media_id": ""}) is True
        assert m.media_ready_now({"upload_mount": "/nas", "media_id": ""}) is True


class TestPauseIsVisible:
    """A worker that is down for a reason must say so. Otherwise status reads
    "stopped", the user starts it by hand, and it fails every job again."""

    def test_status_explains_a_paused_worker(self, tmp_data_dir, capsys):
        m.set_paused("library-unreachable", "/nas/photos")
        with patch.object(m, "read_pid", return_value=None), patch.object(
            m, "log"
        ) as log:
            m.cmd_status(None)
        said = " ".join(str(c) for c in log.warning.call_args_list)
        assert "/nas/photos" in said
        assert "not reachable" in said

    def test_an_explicit_stop_clears_the_marker(self, tmp_data_dir):
        """Otherwise `stop` leaves status blaming the NAS for a worker the user
        turned off on purpose."""
        m.set_paused("library-unreachable", "/nas")
        with patch.object(m, "kill_pid", return_value=False), patch.object(m, "log"):
            m.cmd_stop(None)
        assert m.read_paused() is None

    def test_a_stale_marker_does_not_outlive_the_watcher(self, tmp_data_dir):
        m.set_paused("library-unreachable", "/nas")
        mocks = {
            "load_config": MagicMock(side_effect=[{"worker": True}, KeyboardInterrupt()]),
            "media_ready_now": MagicMock(return_value=True),
            "read_pid": MagicMock(return_value=999),
            "kill_pid": MagicMock(),
            "cmd_start": MagicMock(),
            "cmd_stop": MagicMock(),
            "reconcile_components": MagicMock(),
            "cap_service_logs": MagicMock(),
            "_upgraded_on_disk": MagicMock(return_value=False),
            "log": MagicMock(),
        }
        with patch.multiple(m, **mocks):
            m._watch_worker({"worker": True})
        assert m.read_paused() is None

    def test_the_marker_is_written_when_the_library_drops(self, tmp_data_dir):
        cfg = {"worker": True, "upload_mount": "/nas/photos", "media_id": "x"}
        drive_watch(cfg, [False, False, False])
        marker = m.read_paused()
        assert marker and marker["detail"] == "/nas/photos"


class TestMLRestartDoesNotDowngradeTheEngine:
    """A wedged native engine that is killed and restarted too quickly fails to
    bind, and the caller falls back to the Python venv. The Mac then runs on
    the slow engine indefinitely, with one warning line to show for it.
    """

    def test_a_restart_waits_for_the_port_to_be_released(self):
        busy = [True, True, False]
        with patch.object(m, "port_in_use", side_effect=lambda p: busy.pop(0)), patch.object(
            m.time, "sleep"
        ):
            assert m.wait_for_port_free(3003, timeout=5) is True

    def test_a_port_that_never_frees_is_reported(self):
        with patch.object(m, "port_in_use", return_value=True), patch.object(
            m.time, "sleep"
        ):
            assert m.wait_for_port_free(3003, timeout=0.1) is False

    def test_a_wedged_service_is_not_replaced_while_it_holds_the_port(self):
        """Starting anyway is what silently downgrades the engine."""
        cfg = {"ml": True, "ml_port": 3003}
        with patch.object(m, "_component_enabled", return_value=True), patch.object(
            m, "read_pid", return_value=4242
        ), patch.object(m, "_ml_ping", return_value=False), patch.object(
            m, "kill_pid"
        ), patch.object(
            m, "wait_for_port_free", return_value=False
        ), patch.object(
            m, "_start_ml_service"
        ) as start, patch.object(
            m, "log"
        ), patch.object(
            m, "_ml_unresponsive_since", (4242, 0.0)
        ), patch.object(
            m.time, "monotonic", return_value=10_000.0
        ):
            m.reconcile_ml(cfg)
        start.assert_not_called()


class TestMLAdoption:
    """A pidfile can go missing while the process it named is healthy. The
    watcher then pinged the port, got an answer from a process it had no PID
    for, and classified its own engine as somebody else's service to leave
    alone: status said stopped while ML served, and nothing would have
    restarted it if it wedged. Four days of that on the release Mac.
    """

    NATIVE = "/opt/homebrew/opt/immich-accelerator/libexec/native-ml/immich-ml-native serve 3003"
    VENV = "/opt/homebrew/Cellar/immich-accelerator/1.10.0/libexec/ml/venv/bin/python3.11 -m src.main"
    DOCKER = "/usr/local/bin/com.docker.backend -watchdog -native-api"

    def _ps(self, cmd):
        def run(argv, **kw):
            if argv[0].endswith("lsof"):
                return MagicMock(stdout="64933\n")
            return MagicMock(stdout=cmd)

        return patch.object(m.subprocess, "run", side_effect=run)

    def test_our_native_engine_is_adopted(self, tmp_data_dir):
        with self._ps(self.NATIVE), patch.object(m, "log"):
            assert m.adopt_live_ml({"ml_port": 3003}) == 64933
        assert (tmp_data_dir["pid_dir"] / "ml.pid").exists(), "adoption must persist"

    def test_our_venv_engine_is_adopted(self, tmp_data_dir):
        with self._ps(self.VENV), patch.object(m, "log"):
            assert m.adopt_live_ml({"ml_port": 3003}) == 64933

    def test_somebody_elses_service_is_left_alone(self, tmp_data_dir):
        """Docker's own ML container serves the same port on plenty of setups.
        Adopting it would have us kill and restart a container we do not own."""
        with self._ps(self.DOCKER), patch.object(m, "log"):
            assert m.adopt_live_ml({"ml_port": 3003}) is None

    def test_nothing_listening_is_not_an_adoption(self, tmp_data_dir):
        with patch.object(
            m.subprocess, "run", return_value=MagicMock(stdout="")
        ), patch.object(m, "log"):
            assert m.adopt_live_ml({"ml_port": 3003}) is None

    def test_reconcile_adopts_instead_of_calling_it_foreign(self, tmp_data_dir):
        with patch.object(m, "_component_enabled", return_value=True), patch.object(
            m, "read_pid", return_value=None
        ), patch.object(m, "_ml_ping", return_value=True), patch.object(
            m, "adopt_live_ml", return_value=64933
        ) as adopt, patch.object(
            m, "_start_ml_service"
        ) as start, patch.object(m, "log") as log:
            m.reconcile_ml({"ml": True, "ml_port": 3003})
        adopt.assert_called_once()
        start.assert_not_called()
        said = " ".join(str(c) for c in log.warning.call_args_list)
        assert "already served by something" not in said


class TestPauseNeedsSustainedFailure:
    """A slow NAS and a missing one look identical to one probe, which gets 10
    seconds. Pausing on the first failure stops the worker mid-job and starts
    it again 30 seconds later: the exact thrash this mechanism exists to stop.
    """

    CFG = {
        "worker": True,
        "ml": False,
        "upload_mount": "/nas/photos",
        "media_id": "abc123",
        "mount_recipe": {"fstype": "nfs", "spec": "n:/v", "mountpoint": "/nas"},
    }

    def _drive(self, ready, pids=None):
        return drive_watch(self.CFG, ready, pids=pids)

    def test_one_slow_probe_does_not_stop_the_worker(self, tmp_data_dir):
        """The case that matters: a Synology mid-backup, not a dead mount."""
        mocks = self._drive(ready=[False, True], pids={"worker": 4242})
        assert "worker" not in [c.args[0] for c in mocks["kill_pid"].call_args_list]

    def test_a_remount_is_still_tried_immediately(self, tmp_data_dir):
        """Waiting to act on the worker is not the same as waiting to fix the
        mount. A remount that works during the grace means no pause at all."""
        mocks = self._drive(ready=[False, True])
        mocks["_attempt_remount"].assert_called()

    def test_a_mount_that_stays_gone_still_pauses(self, tmp_data_dir):
        """Hysteresis must not become "never acts"."""
        mocks = self._drive(ready=[False, False, False], pids={"worker": 4242})
        assert "worker" in [c.args[0] for c in mocks["kill_pid"].call_args_list]


class TestNoMountStacking:
    """macOS stacks a second mount on an occupied point without complaint."""

    RECIPE = {"fstype": "nfs", "spec": "n:/v", "mountpoint": "/nas"}

    def test_a_still_mounted_path_is_not_mounted_over(self):
        with patch.object(m, "is_mounted", return_value=True), patch.object(
            m, "remount"
        ) as r, patch.object(m, "log"):
            m._attempt_remount({"mount_recipe": self.RECIPE}, {})
        r.assert_not_called()

    def test_a_genuinely_missing_mount_is_remounted(self):
        with patch.object(m, "is_mounted", return_value=False), patch.object(
            m, "remount", return_value=(True, "")
        ) as r, patch.object(m, "log"):
            m._attempt_remount({"mount_recipe": self.RECIPE}, {})
        r.assert_called_once()

    def test_is_mounted_matches_the_exact_path_only(self):
        out = "n:/v on /nas (nfs)\n/dev/d1 on /nastier (hfs, local)\n"
        with patch.object(
            m.subprocess, "run", return_value=MagicMock(stdout=out, returncode=0)
        ):
            assert m.is_mounted("/nas") is True
            assert m.is_mounted("/nastier") is True
            assert m.is_mounted("/na") is False, "must not match a path prefix"
