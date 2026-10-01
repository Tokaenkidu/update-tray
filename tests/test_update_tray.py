import os, sys, tempfile, subprocess, types, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("DISPLAY", ":97")
import update_tray as ut

class FakeModel(ut.Model):
    def __init__(self): self.__dict__.update(sim={"upgrade": ut.parse_sim(""), "full": ut.parse_sim("")}, report=None, pending=[], checking=False, busy=False, error=None, stages={}, order=[], current=None, total=0,
        session_start=0, offset=0, last_line="", runner=None, reboot=False, done_at=0, installed_n=0, authing=False, log=None)

class T(unittest.TestCase):
    def test_apt_list_regex(self):
        line = "libfoo1/jammy-security 1.2-3 amd64 [upgradable from: 1.2-2]"
        import re
        m = re.match(r"^(\S+?)/(\S+) (\S+) \S+ \[upgradable from: (\S+)\]", line)
        self.assertEqual((m.group(1), m.group(4), m.group(3), "security" in m.group(2)), ("libfoo1", "1.2-2", "1.2-3", True))
        self.assertIsNone(re.match(r"^(\S+?)/(\S+) (\S+) \S+ \[upgradable from: (\S+)\]", "Listing... Done"))
    def test_dpkg_log_stages(self):
        m = FakeModel()
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("2026-10-01 10:00:00 upgrade libfoo1:amd64 1.2-2 1.2-3\n2026-10-01 10:00:01 status half-installed libfoo1:amd64 1.2-2\n"
                    "2026-10-01 10:00:02 status unpacked libfoo1:amd64 1.2-3\n2026-10-01 10:00:03 status half-configured libfoo1:amd64 1.2-3\n"
                    "2026-10-01 10:00:04 status installed libfoo1:amd64 1.2-3\n2026-10-01 10:00:05 upgrade bar:all 1 2\n")
            path = f.name
        ut.DPKG_LOG = path
        m.read_log()
        self.assertEqual(m.stages["libfoo1"], "done"); self.assertEqual(m.stages["bar"], "queued"); self.assertEqual(m.total, 2)
        self.assertAlmostEqual(m.progress(), 0.5)
        os.unlink(path)
    def test_state_priority(self):
        m = FakeModel(); self.assertEqual(m.state(), "uptodate")
        m.pending = [("a", "1", "2", False)]; self.assertEqual(m.state(), "updates")
        m.reboot = True; self.assertEqual(m.state(), "reboot")
        m.error = "x"; self.assertEqual(m.state(), "error")
        m.checking = True; self.assertEqual(m.state(), "checking")
        m.busy = True; self.assertEqual(m.state(), "busy")
    def test_resident_daemons_are_not_busy(self):
        # nothing is installing during the test: must not claim busy because of packagekitd/unattended-upgrade-shutdown
        self.assertFalse(ut.procs_busy())
    def test_log_is_private_and_not_in_tmp(self):
        self.assertFalse(ut.LOG_FILE.startswith("/tmp"))
    def test_single_instance_lock(self):
        a = ut.single_instance(); self.assertIsNotNone(a)
        self.assertIsNone(ut.single_instance())   # a second one is refused
        a.close()
    def test_terminal_fallback_argv(self):
        calls = []
        orig = subprocess.Popen; subprocess.Popen = lambda argv, *a, **k: calls.append(argv)
        import shutil; w = shutil.which; shutil.which = lambda n: "/usr/bin/" + n if n == "xterm" else None
        try: FakeModel().term_fallback(["upgrade"])
        finally: subprocess.Popen = orig; shutil.which = w
        self.assertEqual(calls[0][:4], ["xterm", "-e", "bash", "-c"]); self.assertIn("sudo apt-get upgrade", calls[0][4])
    def test_drawing_runs_for_every_state(self):
        import cairo
        m = FakeModel(); m.pending = [("a", "1", "2", True)] * 3
        for st in ("uptodate", "updates", "checking", "busy", "reboot", "error"):
            m.busy = st == "busy"; m.checking = st == "checking"; m.error = "e" if st == "error" else None; m.reboot = st == "reboot"
            if st == "busy": m.stages = {"a": "unpacking", "b": "configuring", "c": "done"}; m.order = ["a", "b", "c"]; m.total = 3
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 116, 64); ut.draw_icon(cairo.Context(surf), 116, 64, m, 1.5)
            surf2 = cairo.ImageSurface(cairo.FORMAT_ARGB32, 380, 250); ut.draw_panel(cairo.Context(surf2), 380, 250, m, 1.5, rows=5, born=0)

UPGRADE = """Calculating upgrade...
The following upgrades have been deferred due to phasing:
  thermald
The following packages have been kept back:
  libegl-mesa0 libgbm-dev libgbm1 libgbm1:i386 libgl1-mesa-dri:i386
  libgl1-mesa-dri libglx-mesa0
0 to upgrade, 0 to newly install, 0 to remove and 8 not to upgrade.
"""
FULL = """The following NEW packages will be installed
  libwayland-server0:i386
The following packages will be upgraded:
  libegl-mesa0 libgbm-dev
2 to upgrade, 1 to newly install, 0 to remove and 1 not to upgrade.
Inst libegl-mesa0 [25.2.8-0ubuntu0.24.04.2] (25.2.8-0ubuntu0.24.04.3 Ubuntu:24.04/noble-updates [amd64]) []
Inst libwayland-server0:i386 (1.22.0-2.1build1 Ubuntu:24.04/noble [i386]) []
Inst libgbm-dev [25.2.8-0ubuntu0.24.04.2] (25.2.8-0ubuntu0.24.04.3 Ubuntu:24.04/noble-updates [amd64])
"""

class FakeTray:
    """Runs Tray.do_upgrade without a GUI: records the questions it would ask and the apt command it would run."""
    def __init__(self, m, answer=True): self.m, self.answer, self.asked, self.ran = m, answer, [], []
    def ask(self, title, text, ok="OK", cancel=True): self.asked.append((title, ok)); return self.answer
    def confirm(self, what): self.asked.append((what, "confirm")); return self.answer
    do_full = ut.Tray.do_full

class T2(unittest.TestCase):
    def setUp(self):
        self.m = FakeModel(); self.m.pending = [("libegl-mesa0", "1", "2", False)] * 3
        self.m.run_root = lambda args, label, term_cmd=None: self.ran.append(args); self.ran = []
    def test_parse_real_apt_output(self):
        u = ut.parse_sim(UPGRADE); f = ut.parse_sim(FULL)
        self.assertEqual((len(u["install"]), len(u["kept"]), u["phased"]), (0, 7, ["thermald"]))
        self.assertEqual((f["install"], f["new"], f["remove"]), (["libegl-mesa0", "libwayland-server0:i386", "libgbm-dev"], ["libwayland-server0:i386"], []))
    def test_plain_upgrade_with_work_runs_upgrade(self):
        self.m.sim = {"upgrade": {**ut.parse_sim(""), "install": ["a"]}, "full": ut.parse_sim("")}
        t = FakeTray(self.m); ut.Tray.do_upgrade(t); self.assertEqual(self.ran, [["upgrade"]])
    def test_held_back_offers_a_safe_full_upgrade_instead_of_doing_nothing(self):
        self.m.sim = {"upgrade": ut.parse_sim(UPGRADE), "full": ut.parse_sim(FULL)}
        t = FakeTray(self.m); ut.Tray.do_upgrade(t)
        self.assertEqual(self.ran, [["full-upgrade"]]); self.assertEqual(t.asked[0][1], "Run full-upgrade")
    def test_declining_runs_nothing(self):
        self.m.sim = {"upgrade": ut.parse_sim(UPGRADE), "full": ut.parse_sim(FULL)}
        t = FakeTray(self.m, answer=False); ut.Tray.do_upgrade(t); self.assertEqual(self.ran, [])
    def test_full_upgrade_that_removes_packages_is_never_run_silently(self):
        full = ut.parse_sim(FULL + "The following packages will be REMOVED:\n  oldpkg\n")
        self.m.sim = {"upgrade": ut.parse_sim(UPGRADE), "full": full}
        t = FakeTray(self.m); ut.Tray.do_upgrade(t); self.assertEqual(self.ran, []); self.assertIn("needs a decision", t.asked[0][0])
    def test_only_phased_updates_explains_instead_of_running(self):
        self.m.sim = {"upgrade": {**ut.parse_sim(""), "phased": ["thermald"]}, "full": ut.parse_sim("")}
        t = FakeTray(self.m); ut.Tray.do_upgrade(t); self.assertEqual(self.ran, []); self.assertIn("Nothing can be installed", t.asked[0][0])
    def test_explain_sentence_and_version_diff(self):
        self.m.sim = {"upgrade": ut.parse_sim(UPGRADE), "full": ut.parse_sim(FULL)}
        e = self.m.explain(); self.assertIn("7 held back", e); self.assertIn("1 phased", e)
        self.assertEqual(ut.short_ver("25.2.8-0ubuntu0.24.04.2", "25.2.8-0ubuntu0.24.04.3"), ("…04.2", "…04.3"))

if __name__ == "__main__": unittest.main(verbosity=2)
