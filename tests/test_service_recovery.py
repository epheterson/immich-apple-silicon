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
import os
import socket
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import immich_accelerator.__main__ as m

# Captured at import, before no_real_machine_reads replaces it: the tests below
# are the ones that want the real thing.
REAL_ML_PORT_STATE = m.ml_port_state
REAL_PORT_IN_USE = m.port_in_use

MOUNT_OUTPUT = """/dev/disk2s4s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
//eric@nas/photos on /Volumes/photos (smbfs, nodev, nosuid)
//eric@nas/Time%20Machine on /Volumes/Time Machine (smbfs, nobrowse)
nas:/volume1/media on /nas (nfs, nodev)
/dev/disk1s2 on /Volumes/Fast Storage (hfs, local, journaled)
"""


def _mount(output=MOUNT_OUTPUT):
    """Answer /sbin/mount with the table, and let a resolve child resolve.

    The two are different questions and used to share one mock, so the child
    that resolves symlinks was handed the mount table as its answer.
    """

    def run(argv, **kw):
        if argv and str(argv[0]).endswith("mount"):
            return MagicMock(stdout=output, returncode=0)
        if len(argv) >= 3 and "resolve" in str(argv[2]):
            import pathlib as _p

            return MagicMock(stdout=str(_p.Path(argv[3]).resolve()), returncode=0)
        return MagicMock(stdout="", returncode=1)

    return patch.object(m.subprocess, "run", side_effect=run)


def drive_watch(cfg, ready, pids=None, recipe=None, seconds_per_cycle=60.0):
    """Run the real watch loop over a scripted sequence of mount states.

    `ready` is now "is the mount present", read from the mount table, not "did
    a file read succeed". The clock advances a cycle's worth per check, so tests
    can express "gone for two minutes" rather than counting monotonic() calls,
    which the loop makes from several places.
    """
    cfg = dict(cfg)
    cfg.setdefault(
        "mount_recipe", {"fstype": "nfs", "spec": "n:/v", "mountpoint": "/nas"}
    )
    pids = dict(pids or {})
    clock = {"t": 1000.0}
    results = list(ready)

    def mount_state(_point):
        clock["t"] += seconds_per_cycle
        return results.pop(0) if results else True

    mocks = {
        "load_config": MagicMock(
            side_effect=[dict(cfg)] * (len(ready) + 1) + [KeyboardInterrupt()]
        ),
        "is_mounted": MagicMock(side_effect=mount_state),
        "media_io_healthy": MagicMock(return_value=(True, "")),
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

    @pytest.fixture(autouse=True)
    def _mount_is_gone(self):
        """These tests are about the backoff, so the mount must read as absent.
        Without this they consult the real mount table and pass or fail
        depending on whether the developer's machine happens to have the
        recipe's mount point mounted, which is how they passed on a laptop and
        failed on the Mac serving a library."""
        with patch.object(m, "is_mounted", return_value=False):
            yield

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
            "is_mounted": MagicMock(return_value=False),
            "media_io_healthy": MagicMock(return_value=(True, "")),
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

    def test_a_library_with_no_recorded_mount_is_never_judged(self, tmp_data_dir):
        """A local disk, or a library not yet seen healthy, has no absence to
        detect. Judging it anyway is how a working machine gets stopped."""
        assert m.library_mount_gone({}) == (False, "")
        assert m.library_mount_gone({"upload_mount": "/nas"}) == (False, "")

    def test_a_recorded_mount_that_is_present_is_not_gone(self, tmp_data_dir):
        cfg = {"upload_mount": "/nas/immich", "mount_recipe": {"mountpoint": "/nas"}}
        with patch.object(m, "is_mounted", return_value=True):
            assert m.library_mount_gone(cfg) == (False, "/nas")

    def test_a_recorded_mount_missing_from_the_table_is_gone(self, tmp_data_dir):
        cfg = {"upload_mount": "/nas/immich", "mount_recipe": {"mountpoint": "/nas"}}
        with patch.object(m, "is_mounted", return_value=False):
            assert m.library_mount_gone(cfg) == (True, "/nas")

    def test_a_recipe_without_a_library_path_is_not_judged(self, tmp_data_dir):
        """A recipe that cannot be shown to cover the configured library says
        nothing about it."""
        cfg = {"mount_recipe": {"mountpoint": "/nas"}}
        with patch.object(m, "is_mounted", return_value=False):
            assert m.library_mount_gone(cfg) == (False, "")


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
            "is_mounted": MagicMock(return_value=True),
            "media_io_healthy": MagicMock(return_value=(True, "")),
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

    def _ps(self, cmd, lsof_works=True):
        """Model the two shapes of ps this uses, plus lsof.

        `ps -axo pid=,command=` lists every process, which is how an engine of
        ours is found without lsof; `ps -o command= -p PID` identifies one.
        lsof_works=False is the release Mac, where a network mount stalls it.
        """

        def run(argv, **kw):
            if argv[0].endswith("lsof"):
                if not lsof_works:
                    raise subprocess.TimeoutExpired("lsof", 10)
                return MagicMock(stdout="64933\n")
            if "-axo" in argv:
                return MagicMock(stdout=f"64933 {cmd}\n1 /sbin/launchd\n")
            return MagicMock(stdout=cmd)

        return patch.multiple(
            m,
            subprocess=MagicMock(run=MagicMock(side_effect=run), TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError),
            port_in_use=MagicMock(return_value=True),
        )

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


class TestAReadFailureNeverStopsTheWorker:
    """The regression that shipped in 1.11.0, as a test.

    On the release Mac the marker probe failed for ten minutes inside the
    running service while the identical probe succeeded from a shell against
    the same healthy, mounted library. 1.11.0 treated that as "the library is
    gone" and stopped the worker. The mount table said the mount was there the
    whole time.
    """

    CFG = {
        "worker": True,
        "ml": False,
        "upload_mount": "/nas/immich",
        "media_id": "abc",
        "mount_recipe": {"fstype": "nfs", "spec": "n:/v", "mountpoint": "/nas"},
    }

    def _drive(self, io_result, cycles=4):
        mocks = {
            "load_config": MagicMock(
                side_effect=[dict(self.CFG)] * (cycles + 1) + [KeyboardInterrupt()]
            ),
            "is_mounted": MagicMock(return_value=True),
            "media_io_healthy": MagicMock(return_value=io_result),
            "read_pid": MagicMock(return_value=4242),
            "kill_pid": MagicMock(),
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
        return mocks

    def test_a_mounted_library_that_will_not_read_keeps_the_worker_running(
        self, tmp_data_dir
    ):
        mocks = self._drive((False, "probe exited 3"))
        assert "worker" not in [c.args[0] for c in mocks["kill_pid"].call_args_list]
        assert not m.read_paused(), "a read failure is not a pause"

    def test_the_reason_is_logged_so_it_can_be_diagnosed(self, tmp_data_dir):
        """1.11.0 discarded it, which is why the cause took a machine outage to
        even start investigating."""
        mocks = self._drive((False, "probe exited 3"))
        said = " ".join(str(c) for c in mocks["log"].warning.call_args_list)
        assert "probe exited 3" in said

    def test_it_is_said_once_not_every_thirty_seconds(self, tmp_data_dir):
        mocks = self._drive((False, "probe exited 3"), cycles=5)
        hits = [
            c
            for c in mocks["log"].warning.call_args_list
            if "did not read cleanly" in str(c)
        ]
        assert len(hits) == 1, f"logged {len(hits)} times"

    def test_a_healthy_library_says_nothing(self, tmp_data_dir):
        mocks = self._drive((True, ""))
        said = " ".join(str(c) for c in mocks["log"].warning.call_args_list)
        assert "did not read cleanly" not in said


class TestOnlyTheLibrarysOwnMountMaySpeakForIt:
    """Three ways a mount that is not the library's own was allowed to vouch
    for it, all found in review of the 1.11.1 fix.

    The rule they share: the mount recorded while the library was last healthy
    is the only one that may answer, and only while it still covers the
    configured path.
    """

    def test_a_surviving_parent_does_not_vouch_for_a_dropped_nested_mount(self):
        """/nas and /nas/inner are separate mounts and the inner one drops.
        Asking which mount covers the path today answers with the parent, and
        the library path is now a bare directory on it: exactly the placeholder
        the startup gate exists to refuse."""
        cfg = {
            "upload_mount": "/nas/inner/photos",
            "media_id": "x",
            "mount_recipe": {"fstype": "nfs", "spec": "n:/i", "mountpoint": "/nas/inner"},
        }
        # the parent is up, the library's own mount is not
        with patch.object(m, "is_mounted", side_effect=lambda p: p == "/nas"):
            gone, point = m.library_mount_gone(cfg)
        assert gone is True and point == "/nas/inner"

    def test_a_recipe_for_another_path_never_vouches(self):
        """cmd_update rewrites upload_mount from Docker detection and leaves
        mount_recipe untouched, so a recipe can outlive the library it
        described. An unrelated share being mounted says nothing."""
        cfg = {
            "upload_mount": "/srv/photos",
            "media_id": "x",
            "mount_recipe": {"fstype": "smbfs", "spec": "//h/old", "mountpoint": "/Volumes/old"},
        }
        assert m.library_mount(cfg) == ""
        with patch.object(m, "is_mounted", return_value=True) as mounted:
            assert m.library_mount_gone(cfg) == (False, "")
        mounted.assert_not_called(), "a stale recipe must not even be asked about"

    def test_a_library_moved_to_a_local_disk_is_not_paused_forever(self):
        """mount_recipe_for returns None for a local disk, so the refresh guard
        can never overwrite the old recipe. Judging it would pause the worker
        for a mount the library no longer uses, with no way back."""
        cfg = {
            "upload_mount": "/Users/elp/Pictures/immich",
            "media_id": "x",
            "mount_recipe": {"fstype": "nfs", "spec": "n:/v", "mountpoint": "/nas"},
        }
        with patch.object(m, "is_mounted", return_value=False):
            assert m.library_mount_gone(cfg) == (False, "")

    def test_the_libraries_own_mount_still_answers_normally(self):
        cfg = {
            "upload_mount": "/nas/immich",
            "media_id": "x",
            "mount_recipe": {"fstype": "nfs", "spec": "n:/v", "mountpoint": "/nas"},
        }
        with patch.object(m, "is_mounted", return_value=True):
            assert m.library_mount_gone(cfg) == (False, "/nas")
        with patch.object(m, "is_mounted", return_value=False):
            assert m.library_mount_gone(cfg) == (True, "/nas")

    def test_the_startup_gate_refuses_when_the_recorded_mount_is_gone(self, tmp_data_dir):
        """The gate may only wave a timed-out probe through on evidence that
        the library's own mount is present."""
        cfg = {
            "upload_mount": "/nas/immich",
            "media_id": "x",
            "mount_recipe": {"fstype": "nfs", "spec": "n:/v", "mountpoint": "/nas"},
        }
        with patch.object(
            m, "_media_probe", return_value=(False, [], "probe timed out after 15s")
        ), patch.object(m, "is_mounted", return_value=False), patch.object(m, "log"):
            assert m.ensure_media_ready(cfg) is False

    def test_the_startup_gate_proceeds_when_the_recorded_mount_is_present(
        self, tmp_data_dir
    ):
        """The 1.11.1 fix itself: a probe that could not answer is not a probe
        that said no, when the library's mount is demonstrably there."""
        cfg = {
            "upload_mount": "/nas/immich",
            "media_id": "x",
            "mount_recipe": {"fstype": "nfs", "spec": "n:/v", "mountpoint": "/nas"},
        }
        with patch.object(
            m, "_media_probe", return_value=(False, [], "probe timed out after 15s")
        ), patch.object(m, "is_mounted", return_value=True), patch.object(m, "log") as log:
            assert m.ensure_media_ready(cfg) is True
        said = " ".join(str(c) for c in log.warning.call_args_list)
        assert "probe timed out after 15s" in said


class TestNothingTouchesAHungMountInProcess:
    """A hung NFS mount blocks access(2) in the calling thread, uninterruptibly
    and forever. Anything that touches the library path from the watcher's own
    process can therefore wedge the whole service.

    On the release Mac it did exactly that, while holding the start lock, so
    every later start blocked behind it too and the machine could not be
    recovered without killing the process.
    """

    def test_the_writability_check_runs_in_a_child_with_a_timeout(self):
        """The stat above it is already a subprocess for this reason, and the
        next line used to undo that with a bare os.access()."""
        import inspect

        import immich_accelerator.__main__ as mod

        src = inspect.getsource(mod)
        block = src[src.index("# NFS mount reachable") : src.index("# DB connectivity")]
        # Code only: the comment above the fix names the call it replaced.
        code = "\n".join(
            ln for ln in block.splitlines() if not ln.strip().startswith("#")
        )
        assert "os.access(" not in code, (
            "os.access on the library path blocks forever on a hung mount; "
            "use a child process with a timeout"
        )
        assert "timeout=" in code

    def test_no_bare_filesystem_call_on_the_library_path_in_the_watch_loop(self):
        """The loop's own liveness check must stay off the mounted filesystem:
        the mount table answers the question without touching it."""
        import inspect

        import immich_accelerator.__main__ as mod

        raw = inspect.getsource(mod.library_mount_gone) + inspect.getsource(
            mod.library_mount
        )
        src = "\n".join(
            ln for ln in raw.splitlines() if not ln.strip().startswith("#")
        )
        for banned in ("os.access", "os.stat", "os.listdir", ".exists()", ".is_dir()"):
            assert banned not in src, f"{banned} can hang forever on a stale mount"


class TestPortIsAskedBeforeStarting:
    """A start must not race a process that already holds the port.

    The state to keep out of: a pidfile goes missing while the engine it named
    is still serving, the supervisor reads that as ML being absent, and starts a
    replacement that cannot bind. The native engine then exits, and the venv
    fallback goes into the same port.
    """

    CFG = {"ml_port": 3003, "ml_dir": "/tmp", "ml_engine": "native"}

    def _specs(self, mod):
        """Both engines available, so `attempts` is never empty and the port is
        the only thing that can stop a start."""
        return patch.object(
            mod, "_native_ml_spec", lambda cfg, env: (["/usr/bin/true"], "/tmp", env)
        ), patch.object(
            mod, "_venv_ml_spec", lambda cfg, env: (["/usr/bin/true"], "/tmp", env)
        )

    def test_an_occupied_port_stops_the_start(self, tmp_data_dir):
        native, venv = self._specs(m)
        with native, venv, patch.object(
            m, "ml_port_state", return_value=m.PORT_OCCUPIED
        ), patch.object(m, "start_service") as start, patch.object(m, "log"):
            pid, engine, _ = m._start_ml_preferred(dict(self.CFG))
        start.assert_not_called()
        assert pid is None and engine is None

    def test_a_port_we_cannot_inspect_stops_it_too(self, tmp_data_dir):
        """Not knowing is not the same as knowing it is free."""
        native, venv = self._specs(m)
        with native, venv, patch.object(
            m, "ml_port_state", return_value=m.PORT_UNKNOWN
        ), patch.object(m, "start_service") as start, patch.object(m, "log"):
            pid, _, _ = m._start_ml_preferred(dict(self.CFG))
        start.assert_not_called()
        assert pid is None

    def test_a_free_port_still_starts(self, tmp_data_dir):
        """The gate must not swallow the ordinary case."""
        native, venv = self._specs(m)
        with native, venv, patch.object(
            m, "ml_port_state", return_value=m.PORT_FREE
        ), patch.object(m, "start_service", return_value=4321), patch.object(m, "log"):
            pid, engine, is_native = m._start_ml_preferred(dict(self.CFG))
        assert pid == 4321 and is_native

    def test_the_port_is_asked_again_before_the_fallback(self, tmp_data_dir):
        """Asked once for the batch, the venv was started immediately after the
        native engine had just failed to bind the very same port."""
        seen = []

        def state(port):
            seen.append(port)
            return m.PORT_FREE if len(seen) == 1 else m.PORT_OCCUPIED

        native, venv = self._specs(m)
        with native, venv, patch.object(
            m, "ml_port_state", side_effect=state
        ), patch.object(
            m, "start_service", side_effect=RuntimeError
        ) as start, patch.object(m, "log"):
            pid, _, _ = m._start_ml_preferred(dict(self.CFG))
        assert len(seen) == 2, "the port must be re-checked before the second engine"
        # Only the native attempt. Counting the state calls alone would pass even
        # if the second answer were read and ignored.
        assert start.call_count == 1
        assert pid is None



class TestMlPortState:
    """The classifier itself, against real lsof rather than a stub. lsof exits 1
    both when nothing matched and when lsof itself failed, so an empty stdout is
    not on its own an answer."""

    def test_a_real_listener_reads_as_occupied(self):
        """End to end against a socket this test binds itself, rather than
        against whatever the host happens to be running.

        conftest stubs port_in_use for every test, so that a machine serving a
        library does not answer these questions, and this is one of the tests
        its docstring means when it says to patch the behaviour back. Without
        that, the real classifier never runs: it falls through to lsof, which
        finds the listener on a Mac and is absent on the Linux runner, so the
        test passed here and failed there.
        """
        import socket as _s

        with patch.object(m, "port_in_use", REAL_PORT_IN_USE):
            with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as srv:
                srv.bind(("127.0.0.1", 0))
                srv.listen()
                port = srv.getsockname()[1]
                assert REAL_ML_PORT_STATE(port) == m.PORT_OCCUPIED
            assert REAL_ML_PORT_STATE(port) == m.PORT_FREE

    def test_a_listening_port_is_occupied_without_consulting_lsof(self):
        """The network stack answers this in microseconds and cannot be stalled
        by anything on disk. lsof can: it walks every descriptor on the machine,
        so one unresponsive network mount hangs it. On the release Mac, connect
        answers in 0.000s where lsof times out after 10."""
        with patch.object(m, "port_in_use", return_value=True), patch.object(
            m.subprocess, "run", side_effect=AssertionError("must not run lsof")
        ):
            assert REAL_ML_PORT_STATE(3003) == m.PORT_OCCUPIED

    def test_lsof_that_cannot_run_is_free_when_nothing_is_listening(self):
        """A refused connect is evidence, not absence of evidence. Reporting
        unknown here meant an engine could never start on a Mac where lsof
        stalls, and a service that never starts is worse than the double start
        this guard prevents."""
        with patch.object(m, "port_in_use", return_value=False), patch.object(
            m.subprocess, "run", side_effect=OSError
        ):
            assert REAL_ML_PORT_STATE(3003) == m.PORT_FREE

    def test_lsof_reporting_its_own_error_is_not_free(self):
        """Exit 1 with nothing on stdout, which is also how "no match" looks."""
        failed = MagicMock(returncode=1, stdout="", stderr="lsof: no pwd entry\n")
        with patch.object(m.subprocess, "run", return_value=failed):
            assert REAL_ML_PORT_STATE(3003) == m.PORT_UNKNOWN


class TestFallbackHonoursTheWait:
    def test_a_port_still_held_cancels_the_fallback(self, tmp_data_dir):
        """wait_for_port_free's answer used to be discarded, and the venv was
        started into a port the engine just killed had not let go of."""
        with patch.object(m, "_ml_healthy", return_value=False), patch.object(
            m, "kill_pid"
        ), patch.object(m, "wait_for_port_free", return_value=False), patch.object(
            m, "_venv_ml_spec"
        ) as venv, patch.object(m, "log"):
            pid, engine = m._ml_verify_or_fallback(
                {"ml_port": 3003}, 111, "native Swift"
            )
        venv.assert_not_called()
        assert pid is None and engine is None

    def test_a_released_port_still_falls_back(self, tmp_data_dir):
        with patch.object(m, "_ml_healthy", return_value=False), patch.object(
            m, "kill_pid"
        ), patch.object(m, "wait_for_port_free", return_value=True), patch.object(
            m, "_venv_ml_spec", return_value=(["/usr/bin/true"], "/tmp", {})
        ), patch.object(m, "start_service", return_value=222), patch.object(m, "log"):
            pid, engine = m._ml_verify_or_fallback(
                {"ml_port": 3003}, 111, "native Swift"
            )
        assert pid == 222 and engine == "Python venv"


class TestAdoptionRunsBeforeStarting:
    """Both watcher entry points concluded ML was absent from the pidfile alone,
    so a launch while the pidfile was missing started a second engine into the
    port the first one was still serving."""

    def test_watch_worker_adopts_instead_of_starting(self, tmp_data_dir):
        """The worker loop too: a live worker plus a missing ml.pid used to run
        a full cmd_start, which restarted the worker as well."""
        with patch.object(m, "adopt_live_ml", return_value=64933):
            mocks = drive_watch(
                {"worker": True, "ml": True}, ready=[True], pids={"worker": 111}
            )
        mocks["cmd_start"].assert_not_called()

    def test_watch_without_worker_adopts_instead_of_starting(self, tmp_data_dir):
        with patch.object(m, "read_pid", return_value=None), patch.object(
            m, "adopt_live_ml", return_value=64933
        ), patch.object(m, "reconcile_dashboard"), patch.object(
            m, "_start_without_worker"
        ) as start, patch("signal.signal"), patch(
            "time.sleep", side_effect=KeyboardInterrupt
        ):
            m._watch_without_worker({"ml_port": 3003, "ml": True})
        start.assert_not_called()

    def test_ml_switched_off_is_not_adopted(self, tmp_data_dir):
        """With ML off, _start_without_worker is what stops a leftover engine.
        Adopting one instead would leave it running until the next cycle, and
        report it as ours in the meantime."""
        with patch.object(m, "read_pid", return_value=None), patch.object(
            m, "adopt_live_ml", return_value=64933
        ) as adopt, patch.object(m, "reconcile_dashboard"), patch.object(
            m, "_start_without_worker"
        ) as start, patch("signal.signal"), patch(
            "time.sleep", side_effect=KeyboardInterrupt
        ):
            m._watch_without_worker({"ml_port": 3003, "ml": False})
        adopt.assert_not_called()
        start.assert_called_once()


class TestSpawnedServicesLeadTheirOwnSession:
    """_signal_service decides by session leadership, so everything we spawn has
    to be a session leader or its children stop going with it. Checked on a live
    install for worker, ml and dashboard; this keeps start_new_session from
    being dropped from start_service without anything noticing."""

    def test_start_service_leaves_a_session_leader(self, tmp_data_dir):
        with patch.object(m.time, "sleep"):  # skip the liveness pause
            pid = m.start_service("ml", ["/bin/sleep", "30"], dict(os.environ), "/tmp")
        try:
            assert os.getsid(pid) == pid
        finally:
            os.kill(pid, 9)


class TestTheGroupIsSignalledOnlyWhenWeLeadTheSession:
    """start_service and start_dashboard pass start_new_session, so a group we
    created has its leader as the session leader, and only our descendants can
    join it. A process started from a shell leads its job's group but not the
    session, so an adopted one is signalled alone.

    The test is on the group, not on the pid. Asking whether this pid is itself
    the session leader is false for the pid the worker adoption path returns:
    _find_live_worker_pid hands back a child, not the leader. The group then
    went unsignalled and ffmpeg and exiftool survived a stop, because
    _kill_all_worker_processes matches only immich and node.
    """

    def test_a_session_leader_takes_the_group(self, tmp_data_dir):
        (tmp_data_dir["pid_dir"] / "ml.pid").write_text("4242\n")
        with patch.object(m.os, "getsid", return_value=4242), patch.object(
            m.os, "getpgid", return_value=4242
        ), patch.object(
            m, "_group_leader_is_ours", return_value=True
        ), patch.object(m.os, "killpg") as killpg, patch.object(
            m.os, "kill", side_effect=OSError()
        ), patch.object(m, "read_pid", return_value=4242):
            m.kill_pid("ml")
        assert killpg.call_args_list[0][0][0] == 4242

    def test_an_adopted_child_of_ours_still_takes_the_group(self, tmp_data_dir):
        """The regression this class exists for. The pid is not the leader, but
        its group was made by setsid, so the group is ours and the worker's
        ffmpeg and exiftool children have to go with it."""
        (tmp_data_dir["pid_dir"] / "worker.pid").write_text("4242\n")
        with patch.object(m.os, "getsid", return_value=900), patch.object(
            m.os, "getpgid", return_value=900
        ), patch.object(
            m, "_group_leader_is_ours", return_value=True
        ), patch.object(m.os, "killpg") as killpg, patch.object(
            m.os, "kill", side_effect=OSError()
        ), patch.object(m, "read_pid", return_value=4242), patch.object(
            m, "_kill_all_worker_processes"
        ):
            m.kill_pid("worker")
        assert killpg.call_args_list[0][0][0] == 900

    def test_a_group_led_by_a_stranger_is_never_group_killed(self, tmp_data_dir):
        """The predicate is satisfied by plenty of processes nobody here
        started: on one Mac, fifteen more than satisfy "is itself the leader",
        including another supervisor and its children. Worker adoption matches
        a worker-shaped process anywhere with no ownership check, so without
        this an adopted stranger took its whole group with it."""
        (tmp_data_dir["pid_dir"] / "worker.pid").write_text("4242\n")
        with patch.object(m.os, "getsid", return_value=900), patch.object(
            m.os, "getpgid", return_value=900
        ), patch.object(
            m, "_group_leader_is_ours", return_value=False
        ), patch.object(m.os, "killpg") as killpg, patch.object(
            m.os, "kill", side_effect=[None, OSError()]
        ) as kill, patch.object(m, "read_pid", return_value=4242), patch.object(
            m, "_kill_all_worker_processes"
        ):
            m.kill_pid("worker")
        killpg.assert_not_called()
        assert kill.call_args_list[0][0][0] == 4242

    def test_a_pid_inside_somebody_elses_session_is_signalled_alone(self, tmp_data_dir):
        """A shell job: its group leader is not the session leader."""
        (tmp_data_dir["pid_dir"] / "ml.pid").write_text("4242\n")
        with patch.object(m.os, "getsid", return_value=900), patch.object(
            m.os, "getpgid", return_value=4242
        ), patch.object(m.os, "killpg") as killpg, patch.object(
            m.os, "kill", side_effect=[None, OSError()]
        ) as kill, patch.object(m, "read_pid", return_value=4242):
            m.kill_pid("ml")
        killpg.assert_not_called()
        assert kill.call_args_list[0][0][0] == 4242

    def test_the_worker_sweep_still_runs(self, tmp_data_dir):
        """The worker keeps its own sweep either way: it is the one service with
        descendants that outlive their parent."""
        (tmp_data_dir["pid_dir"] / "worker.pid").write_text("4242\n")
        with patch.object(m.os, "getsid", return_value=900), patch.object(
            m.os, "getpgid", return_value=4242
        ), patch.object(m.os, "killpg"), patch.object(
            m.os, "kill", side_effect=OSError()
        ), patch.object(m, "read_pid", return_value=4242), patch.object(
            m, "_kill_all_worker_processes"
        ) as sweep:
            m.kill_pid("worker")
        sweep.assert_called_once()


class TestOneVenvWorker:
    def test_web_concurrency_is_pinned(self, tmp_path):
        """Uvicorn reads WEB_CONCURRENCY and the supervisor passes its own
        environment through, so an inherited value would fork a second copy of
        the service, each loading its own multi-gigabyte model."""
        venv = tmp_path / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python3").write_text("")
        spec = m._venv_ml_spec({"ml_dir": str(tmp_path)}, {"WEB_CONCURRENCY": "4"})
        assert spec is not None
        assert spec[2]["WEB_CONCURRENCY"] == "1"


class TestPortChecksSurviveAnUnusableLsof:
    """lsof walks every open descriptor on the machine, so a single
    unresponsive network mount stalls it past any timeout. The release Mac is
    that machine: a TCP connect answers there in 0.000s while lsof times out
    after 10.

    Both of these were decided with lsof, and on that Mac the result was an
    engine that could never start and a running engine that could never be
    adopted, which is worse than either problem they were added to solve.
    """

    NATIVE = "/opt/homebrew/opt/immich-accelerator/libexec/native-ml/immich-ml-native serve 3003"

    def test_a_held_port_reads_as_occupied_when_lsof_cannot_answer(self):
        with patch.object(m, "port_in_use", return_value=True), patch.object(
            m.subprocess, "run", side_effect=subprocess.TimeoutExpired("lsof", 10)
        ):
            assert REAL_ML_PORT_STATE(3003) == m.PORT_OCCUPIED

    def test_our_engine_is_still_adopted_when_lsof_cannot_answer(self, tmp_data_dir):
        def run(argv, **kw):
            if argv[0].endswith("lsof"):
                raise subprocess.TimeoutExpired("lsof", 10)
            if "-axo" in argv:
                return MagicMock(stdout=f"64933 {self.NATIVE}\n1 /sbin/launchd\n")
            return MagicMock(stdout=self.NATIVE)

        with patch.object(m, "port_in_use", return_value=True), patch.object(
            m.subprocess, "run", side_effect=run
        ), patch.object(m, "log"):
            assert m.adopt_live_ml({"ml_port": 3003}) == 64933

    def test_nothing_is_adopted_when_the_port_is_not_even_held(self, tmp_data_dir):
        """The process scan says an engine of ours is running, not that it owns
        the port, so the port has to be established first. The subprocess
        side_effect is what makes this non-vacuous: reaching it at all is the
        failure."""
        with patch.object(m, "port_in_use", return_value=False), patch.object(
            m.subprocess, "run", side_effect=AssertionError("must not be reached")
        ), patch.object(m, "log"):
            assert m.adopt_live_ml({"ml_port": 3003}) is None

    def test_the_common_case_never_waits_on_lsof(self):
        """Ours-first, from the process table. Asking lsof first cost ten
        seconds per call on the release Mac before falling back to exactly this
        answer, and the watcher makes this call on a timer."""
        native = TestPortChecksSurviveAnUnusableLsof.NATIVE

        def run(argv, **kw):
            if argv[0].endswith("lsof"):
                raise AssertionError("lsof must not be consulted for our own engine")
            if "-axo" in argv:
                return MagicMock(stdout=f"64933 {native}\n")
            return MagicMock(stdout=native)

        with patch.object(m, "port_in_use", return_value=True), patch.object(
            m.subprocess, "run", side_effect=run
        ), patch.object(m, "log"):
            assert m.adopt_live_ml({"ml_port": 3003}) == 64933


class TestADeadMountCannotWedgeTheWatcher:
    """Every path check the watch loop makes has to survive a mount whose
    server has gone away. Those calls do not fail, and they do not time out;
    they never return.

    This has now happened twice on the release Mac, in two different calls.
    os.access in the start path took the watcher down while it held the start
    lock. Path.resolve() in mount_recipe_for took it down when the NAS went off
    for the night, at the moment its whole job was to notice that.
    """

    def test_the_recipe_lookup_does_not_resolve_in_this_process(self):
        """resolve() lstats every component of the path."""
        import inspect

        src = "\n".join(
            ln
            for ln in inspect.getsource(m.mount_recipe_for).splitlines()
            if not ln.strip().startswith("#")
        )
        assert ".resolve()" not in src, (
            "resolve() on the library path never returns on a dead mount; "
            "use _resolve_offthread"
        )

    def test_a_plain_match_needs_no_resolving_at_all(self):
        """The common case, and the one that matters when the mount is dead:
        /nas/immich under a mount at /nas matches without touching the disk."""
        out = "10.0.0.14:/volume1/ELP NAS on /nas (nfs)\n"
        with patch.object(m.subprocess, "run") as run:
            run.side_effect = lambda argv, **kw: (
                MagicMock(stdout=out, returncode=0)
                if str(argv[0]).endswith("mount")
                else (_ for _ in ()).throw(AssertionError("must not resolve"))
            )
            r = m.mount_recipe_for("/nas/immich")
        assert r and r["mountpoint"] == "/nas"

    def test_a_resolver_that_hangs_is_bounded(self):
        with patch.object(
            m.subprocess, "run", side_effect=subprocess.TimeoutExpired("py", 5)
        ):
            assert m._resolve_offthread("/nas/immich") is None


class TestTheWorkerWaitsWhenItsDatabaseIsGone:
    """On a split install Postgres and Redis live on the same box as the
    library, so when that box goes away the worker can do nothing at all. It
    used to sit reconnecting all night, filling its log with one error, while
    status reported it running.

    A TCP connect decides this. That is the distinction 1.11.1 was about: a
    connect answers in milliseconds and cannot hang, unlike the file read that
    stopped a healthy machine.
    """

    CFG = {
        "worker": True,
        "ml": False,
        "db_hostname": "10.0.0.14", "db_port": 15432,
        "redis_hostname": "10.0.0.14", "redis_port": 16379,
    }

    def _drive(self, down_seq, pids=None, cycles=None):
        pids = dict(pids or {})
        clock = {"t": 1000.0}
        seq = list(down_seq)

        def backends(_cfg):
            clock["t"] += 60
            return seq.pop(0) if seq else []

        n = cycles or len(down_seq)
        mocks = {
            "load_config": MagicMock(
                side_effect=[dict(self.CFG)] * (n + 1) + [KeyboardInterrupt()]
            ),
            "backends_down": MagicMock(side_effect=backends),
            "library_mount_gone": MagicMock(return_value=(False, "")),
            "media_io_healthy": MagicMock(return_value=(True, "")),
            "is_mounted": MagicMock(return_value=True),
            "read_pid": MagicMock(side_effect=lambda x: pids.get(x)),
            "kill_pid": MagicMock(side_effect=lambda x, **k: pids.pop(x, None)),
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
        with patch.multiple(m, **mocks), patch.object(
            m.time, "monotonic", side_effect=lambda: clock["t"]
        ):
            m._watch_worker(dict(self.CFG))
        return mocks

    def test_a_brief_blip_does_not_stop_the_worker(self, tmp_data_dir):
        """One missed connect is a network hiccup, not an outage."""
        mocks = self._drive([["Postgres"], []], pids={"worker": 4242})
        assert "worker" not in [c.args[0] for c in mocks["kill_pid"].call_args_list]
        assert not m.read_paused()

    def test_a_real_outage_pauses_the_worker(self, tmp_data_dir):
        mocks = self._drive([["Postgres", "Redis"]] * 4, pids={"worker": 4242})
        assert "worker" in [c.args[0] for c in mocks["kill_pid"].call_args_list]
        marker = m.read_paused()
        assert marker and marker["reason"] == "backend-unreachable"
        assert "Postgres" in marker["detail"]

    def test_it_starts_again_when_they_come_back(self, tmp_data_dir):
        mocks = self._drive([["Postgres", "Redis"]] * 3 + [[], []], pids={"worker": 4242})
        assert mocks["cmd_start"].called, "must come back without anyone helping"
        assert not m.read_paused(), "and the marker must be cleared"

    def test_an_ml_only_node_is_never_judged(self, tmp_data_dir):
        """It has no database configured and does not want one."""
        assert m.backends_down({"ml_only": True}) == []

    def test_status_says_which_service_is_missing(self, tmp_data_dir):
        m.set_paused("backend-unreachable", "Postgres, Redis")
        with patch.object(m, "read_pid", return_value=None), patch.object(m, "log") as log:
            m.cmd_status(None)
        said = " ".join(str(c) for c in log.warning.call_args_list)
        assert "Postgres, Redis" in said and "not answering" in said


class TestWeOnlyAdoptTheEngineHoldingOurPort:
    """"Something holds ml_port" and "an engine of ours is running somewhere"
    are not the same fact. Treating them as one adopts the wrong process.

    Both halves happen on the release Mac at once: Docker's own ML container is
    a real holder of 3003, and the preflight gate, which policy says to run on
    that same Mac, starts an engine of ours on a different port.
    """

    NATIVE_3003 = "/opt/homebrew/.../native-ml/immich-ml-native serve 3003"
    NATIVE_3998 = "/opt/homebrew/.../native-ml/immich-ml-native serve 3998"

    def _ps(self, cmd):
        def run(argv, **kw):
            if argv[0].endswith("lsof"):
                raise subprocess.TimeoutExpired("lsof", 10)
            if "-axo" in argv:
                return MagicMock(stdout=f"64933 {cmd}\n1 /sbin/launchd\n")
            return MagicMock(stdout=cmd)

        return patch.object(m.subprocess, "run", side_effect=run)

    def test_an_engine_on_another_port_is_not_adopted(self, tmp_data_dir):
        """The preflight gate's own server, while Docker holds 3003. Adopting it
        meant the watcher could later signal a process it never started, and
        fail the one gate that protects releases."""
        with self._ps(self.NATIVE_3998), patch.object(
            m, "port_in_use", return_value=True
        ), patch.object(m, "log"):
            assert m.adopt_live_ml({"ml_port": 3003}) is None

    def test_the_engine_on_our_port_is_adopted(self, tmp_data_dir):
        with self._ps(self.NATIVE_3003), patch.object(
            m, "port_in_use", return_value=True
        ), patch.object(m, "log"):
            assert m.adopt_live_ml({"ml_port": 3003}) == 64933


class TestTwoPauseReasonsShareOneMarker:
    """The library pause and the database pause stop the same worker and write
    the same marker file, and a NAS going away triggers both: the mount drops
    and the Postgres and Redis on that box stop answering.

    Postgres answers again the moment the box is back. The mount often does not,
    which is why the remount exists. So the database resume routinely arrives
    while the library pause is still in force.
    """

    CFG = {
        "worker": True,
        "ml": False,
        "upload_mount": "/nas/immich",
        "media_id": "abc",
        "mount_recipe": {"fstype": "nfs", "spec": "n:/v", "mountpoint": "/nas"},
        "db_hostname": "10.0.0.14", "db_port": 15432,
        "redis_hostname": "10.0.0.14", "redis_port": 16379,
    }

    def _drive(self, gone_seq, down_seq, pids=None):
        """A worker is running to begin with, so the only cmd_start that can
        appear is a resume: the pre-loop start fires when no pid is found."""
        pids = dict(pids if pids is not None else {"worker": 4242})
        clock = {"t": 1000.0}
        gone, down = list(gone_seq), list(down_seq)
        n = max(len(gone_seq), len(down_seq))

        def mount_gone(_cfg):
            clock["t"] += 60
            return (gone.pop(0), "/nas") if gone else (False, "/nas")

        mocks = {
            "load_config": MagicMock(
                side_effect=[dict(self.CFG)] * (n + 1) + [KeyboardInterrupt()]
            ),
            "library_mount_gone": MagicMock(side_effect=mount_gone),
            "backends_down": MagicMock(
                side_effect=lambda _c: down.pop(0) if down else []
            ),
            "media_io_healthy": MagicMock(return_value=(True, "")),
            "is_mounted": MagicMock(return_value=True),
            "read_pid": MagicMock(side_effect=lambda n: pids.get(n)),
            "kill_pid": MagicMock(side_effect=lambda n, **k: pids.pop(n, None)),
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
        with patch.multiple(m, **mocks), patch.object(
            m.time, "monotonic", side_effect=lambda: clock["t"]
        ):
            m._watch_worker(dict(self.CFG))
        return mocks

    def test_the_database_coming_back_does_not_erase_a_library_pause(
        self, tmp_data_dir
    ):
        """The whole box goes, then returns, but the share is not remounted yet.
        Clearing the marker here left status saying "stopped" with no reason for
        the rest of the outage, which is the failure the marker prevents."""
        mocks = self._drive(
            gone_seq=[True] * 8,                       # mount gone throughout
            down_seq=[[], [], ["Postgres"], ["Postgres"], ["Postgres"], [], [], []],
        )
        marker = m.read_paused()
        assert marker, "a worker held down must say why"
        assert marker["reason"] == "library-unreachable"
        mocks["cmd_start"].assert_not_called()

    def test_both_clearing_starts_the_worker_once(self, tmp_data_dir):
        mocks = self._drive(
            gone_seq=[True, True, True, False, False],
            down_seq=[["Postgres"]] * 3 + [[], []],
        )
        assert not m.read_paused(), "marker cleared once both are back"

    def test_a_local_disk_library_never_forks_a_resolver(self):
        """Most installs keep the library on the internal disk, where no
        remountable mount can ever match. Falling through to the child resolver
        anyway spawned an interpreter every cycle, thousands a day, for a result
        that is discarded."""
        local_only = "/dev/disk2s4s1 on / (apfs, sealed, local)\n"

        def run(argv, **kw):
            if str(argv[0]).endswith("mount"):
                return MagicMock(stdout=local_only, returncode=0)
            raise AssertionError("must not spawn a resolver for a local library")

        with patch.object(m.subprocess, "run", side_effect=run):
            assert m.mount_recipe_for("/Users/elp/Pictures/immich") is None


class TestSettingsReachableOnAHomebrewInstall:
    """The IMMICH_ACCEL* variables are documented, and on the standard install
    there was no supported way to set any of them: brew services generates the
    plist, it carries no EnvironmentVariables, launchctl setenv does not reach
    the agent, and a hand edit is undone by the next restart. The only way
    through was wrapping the binary in a script. Reported by RxChi1d on #137.
    """

    def test_a_setting_in_config_reaches_a_spawned_service(self, tmp_data_dir):
        cfg = {"env": {"IMMICH_ACCELERATOR_HEIC_DECODE_CONCURRENCY": "3"}}
        with patch.object(m, "load_config", return_value=cfg), patch.object(
            m.subprocess, "Popen"
        ) as popen, patch.object(m, "write_pid"), patch.object(
            m.time, "sleep"
        ), patch.object(m, "log"):
            popen.return_value = MagicMock(pid=4242, poll=MagicMock(return_value=None))
            m.start_service("worker", ["/bin/true"], {"PATH": "/usr/bin"}, "/tmp")
        passed = popen.call_args.kwargs["env"]
        assert passed["IMMICH_ACCELERATOR_HEIC_DECODE_CONCURRENCY"] == "3"
        assert passed["PATH"] == "/usr/bin", "the rest of the environment survives"

    def test_only_our_own_variables_can_be_set_there(self, tmp_data_dir):
        """Everything else a service needs is worked out here. A config file
        that could override the database or the port would be a way to break an
        install from a place nobody thinks to look."""
        cfg = {"env": {"DB_PASSWORD": "nope", "PATH": "/evil", "IMMICH_ACCEL_X": "1"}}
        with patch.object(m, "log"):
            got = m.config_env(cfg)
        assert got == {"IMMICH_ACCEL_X": "1"}

    def test_no_config_at_all_is_not_an_error(self, tmp_data_dir):
        """start_service runs before setup has written anything."""
        with patch.object(m, "load_config", side_effect=RuntimeError("not set up")):
            assert m.config_env() == {}

    def test_the_watcher_reads_its_own_knobs_from_config(self, tmp_data_dir):
        """These are bound at import, so a value in config could never reach
        them however it was set."""
        cfg = {"env": {"IMMICH_ACCEL_FD_RESTART_THRESHOLD": "500"}}
        assert m.int_setting("IMMICH_ACCEL_FD_RESTART_THRESHOLD", 10000, cfg) == 500

    def test_a_value_that_is_not_a_number_falls_back(self, tmp_data_dir):
        cfg = {"env": {"IMMICH_ACCEL_FD_RESTART_THRESHOLD": "lots"}}
        with patch.object(m, "log"):
            assert m.int_setting("IMMICH_ACCEL_FD_RESTART_THRESHOLD", 10000, cfg) == 10000
