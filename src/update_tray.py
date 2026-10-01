#!/usr/bin/env python3
"""Update Tray - animated system-update icon for the panel tray (Debian/Ubuntu/Mint, apt).
Shows pending updates, watches ANY running apt/dpkg session (Update Manager, terminal, unattended),
tracks which package is being unpacked/configured, and can run update/upgrade via pkexec."""
import gi, math, os, re, time, threading, subprocess, json
gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib
import cairo

DPKG_LOG = "/var/log/dpkg.log"
LOG_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "update-tray")
LOG_FILE = os.path.join(LOG_DIR, "update-tray.log")   # private to the user (a fixed file in /tmp could be hijacked by a symlink)
import colorsys, random
COL = {"uptodate": (0.66, 0.95, 0.80), "updates": (1.0, 0.85, 0.66), "checking": (0.62, 0.86, 1.0),
       "busy": (0.78, 0.70, 1.0), "reboot": (1.0, 0.72, 0.68), "error": (1.0, 0.65, 0.75)}
STAGE_COL = {"queued": (.72, .74, .86), "unpacking": (1.0, .86, .62), "configuring": (.62, .86, 1.0), "done": (.66, .96, .80)}

def pastel(h, s=0.42, v=1.0):
    return colorsys.hsv_to_rgb(h % 1.0, s, v)

def _pr(n, k=0): return (math.sin(n * 12.9898 + k * 78.233) * 43758.5453) % 1.0

def sparkles(c, x, y, w, h, t, n=6, col=(1, 1, 1), size=5):
    for k in range(n):
        a = max(0, math.sin(t * (1.5 + _pr(k, 1) * 2.5) + _pr(k, 2) * 6.28)) ** 5
        px, py, r = x + w * _pr(k, 3), y + h * _pr(k, 4), size * a
        c.set_source_rgba(*col, a); c.set_line_width(max(1, size * .28))
        c.move_to(px - r, py); c.line_to(px + r, py); c.move_to(px, py - r); c.line_to(px, py + r); c.stroke()
        c.arc(px, py, r * .25, 0, 6.283); c.fill()

def aurora(c, W, H, t):
    """Soft moving pastel blobs used as animated background."""
    for k, hue in enumerate((0.0, 0.33, 0.62, 0.85)):
        cx = W * (.5 + .45 * math.sin(t * .4 + k * 1.7)); cy = H * (.5 + .45 * math.cos(t * .33 + k * 2.3))
        g = cairo.RadialGradient(cx, cy, 0, cx, cy, max(W, H) * .5); r, gg, b = pastel(hue + t * .02, .5, 1)
        g.add_color_stop_rgba(0, r, gg, b, .16); g.add_color_stop_rgba(1, r, gg, b, 0)
        c.set_source(g); c.paint()

def confetti(c, W, H, age):
    if age > 6: return
    fade = 1 - max(0, (age - 4.5) / 1.5)
    for k in range(60):
        x0 = W * _pr(k, 1); sp = .5 + .9 * _pr(k, 2); y = -20 + (age * sp * H * .55) % (H + 40) if age * sp * H * .55 < H + 40 else H + 99
        if y > H + 40: continue
        x = x0 + 22 * math.sin(age * 3 + k); r, g, b = pastel(_pr(k, 3), .5)
        c.save(); c.translate(x, y); c.rotate(age * 4 * (_pr(k, 4) - .5) + k); c.set_source_rgba(r, g, b, .95 * fade)
        c.rectangle(-4, -2, 8, 4); c.fill(); c.restore()

def procs_busy():
    """True only while a package operation is really running (ignores resident daemons and read-only apt queries)."""
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me: continue
        try:
            with open(f"/proc/{pid}/comm") as f: comm = f.read().strip()
            if comm not in ("apt", "apt-get", "dpkg", "aptitude", "unattended-upgr"): continue
            with open(f"/proc/{pid}/cmdline") as f: cmd = f.read().replace("\0", " ")
        except Exception: continue
        if "unattended-upgrade-shutdown" in cmd: continue
        if comm == "dpkg" or comm == "aptitude": return True
        if comm == "unattended-upgr": return True
        if comm in ("apt", "apt-get") and re.search(r"\b(install|upgrade|full-upgrade|dist-upgrade|update|remove|purge|autoremove|reinstall)\b", cmd): return True
    return False

def rrect(c, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    c.new_sub_path()
    c.arc(x+w-r, y+r, r, -math.pi/2, 0); c.arc(x+w-r, y+h-r, r, 0, math.pi/2)
    c.arc(x+r, y+h-r, r, math.pi/2, math.pi); c.arc(x+r, y+r, r, math.pi, 3*math.pi/2)
    c.close_path()

def notify(msg):
    try: subprocess.Popen(["notify-send", "-i", "system-software-update", "Update Tray", msg])
    except Exception: pass

# ------------------------------------------------------------------ model
class Model:
    def __init__(self):
        self.pending = []            # [(name, old, new, security)]
        self.checking = False; self.busy = False; self.error = None
        self.stages = {}; self.order = []; self.current = None; self.total = 0
        self.session_start = 0; self.offset = 0; self.last_line = ""; self.runner = None
        self.reboot = os.path.exists("/var/run/reboot-required")
        self.done_at = 0; self.installed_n = 0; self.authing = False; self.log = None
        self.refresh_pending()

    # ---- pending list (no root needed) ----
    def refresh_pending(self):
        if self.checking: return
        self.checking = True
        def work():
            try:
                out = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True,
                                     env=dict(os.environ, LC_ALL="C"), timeout=120).stdout
                res = []
                for ln in out.splitlines():
                    m = re.match(r"^(\S+?)/(\S+) (\S+) \S+ \[upgradable from: (\S+)\]", ln)
                    if m: res.append((m.group(1), m.group(4), m.group(3), "security" in m.group(2)))
                GLib.idle_add(self.set_pending, res)
            except Exception as e:
                GLib.idle_add(self.set_pending, None, str(e))
        threading.Thread(target=work, daemon=True).start()
    def set_pending(self, res, err=None):
        self.checking = False
        if res is None: self.error = err
        else: self.pending = res; self.error = None
        self.reboot = os.path.exists("/var/run/reboot-required"); return False

    # ---- watch apt/dpkg activity ----
    def procs_busy(self): return procs_busy()
    def poll(self):
        b = self.procs_busy() or (self.runner is not None and self.runner.poll() is None)
        if b and not self.busy:                                   # session starts
            self.busy = True; self.session_start = time.time(); self.stages = {}; self.order = []; self.current = None
            self.total = len(self.pending)
            try: self.offset = os.path.getsize(DPKG_LOG)
            except Exception: self.offset = 0
            for p in self.pending: self.stages[p[0]] = "queued"; self.order.append(p[0])
        if self.busy:
            self.read_log()
            if not b:                                             # session ends
                self.busy = False; self.done_at = time.time()
                n = sum(1 for v in self.stages.values() if v == "done"); self.installed_n = n
                if n: notify(f"Installed {n} package{'s' if n != 1 else ''}.")
                self.refresh_pending()
        return True
    def read_log(self):
        try:
            sz = os.path.getsize(DPKG_LOG)
            if sz < self.offset: self.offset = 0
            with open(DPKG_LOG, "r", errors="ignore") as f:
                f.seek(self.offset); data = f.read(); self.offset = f.tell()
        except Exception: return
        for ln in data.splitlines():
            p = ln.split()
            if len(p) < 5: continue
            kind = p[2]
            if kind == "status":
                name = p[4].split(":")[0]; st = p[3]; self.current = name
                if name not in self.stages: self.order.append(name)
                if st in ("half-installed", "unpacked"): self.stages[name] = "unpacking"
                elif st == "half-configured": self.stages[name] = "configuring"
                elif st == "installed": self.stages[name] = "done"
            elif kind in ("upgrade", "install") and len(p) >= 4:
                name = p[3].split(":")[0]
                if name not in self.stages: self.order.append(name)
                self.stages.setdefault(name, "queued")
        self.total = max(self.total, len(self.stages))
    def progress(self):
        if not self.total: return 0.0
        done = sum(1 for v in self.stages.values() if v == "done")
        part = sum(0.5 for v in self.stages.values() if v in ("unpacking", "configuring"))
        return min(1.0, (done + part * 0.6) / self.total)
    def state(self):
        if self.busy: return "busy"
        if self.checking: return "checking"
        if self.error: return "error"
        if self.reboot: return "reboot"
        return "updates" if self.pending else "uptodate"

    # ---- run apt via polkit ----
    def run_root(self, args, label, term_cmd=None):
        if self.runner and self.runner.poll() is None: return
        self.error = None; self.last_line = "Waiting for your password\u2026"; self.authing = True
        os.makedirs(LOG_DIR, mode=0o700, exist_ok=True); self.log = open(LOG_FILE, "a"); self.log.write(f"\n--- {time.ctime()} apt-get {' '.join(args)}\n"); self.log.flush()
        try:
            self.runner = subprocess.Popen(["pkexec", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-y", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold", *args],
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            threading.Thread(target=self._pump, args=(self.runner,), daemon=True).start()
        except Exception as e:
            self.error = str(e); self.term_fallback(term_cmd or args); return
        def watchdog():                               # no password prompt / no activity -> visible terminal instead
            if self.authing and self.runner and self.runner.poll() is None and not self.busy:
                try: self.runner.kill()
                except Exception: pass
                self.last_line = "No password prompt appeared \u2014 opening a terminal"; self.term_fallback(term_cmd or args)
            return False
        GLib.timeout_add_seconds(45, watchdog)
    def term_fallback(self, args):
        import shutil
        cmd = "sudo apt-get " + " ".join(args) + "; echo; read -p 'Finished. Press Enter to close.'"
        argv = {"gnome-terminal": ["--", "bash", "-c", cmd], "mate-terminal": ["-x", "bash", "-c", cmd], "xfce4-terminal": ["-x", "bash", "-c", cmd],
                "x-terminal-emulator": ["-e", "bash", "-c", cmd], "xterm": ["-e", "bash", "-c", cmd]}
        for term in ("mate-terminal", "x-terminal-emulator", "xfce4-terminal", "gnome-terminal", "xterm"):
            if shutil.which(term):
                subprocess.Popen([term, *argv[term]]); return
        notify("No terminal found - run: sudo apt-get " + " ".join(args))
    def _pump(self, proc):
        for ln in proc.stdout:
            ln = ln.strip()
            if ln:
                self.authing = False; self.last_line = ln[:90]
                try: self.log.write(ln + "\n"); self.log.flush()
                except Exception: pass
        rc = proc.wait(); self.authing = False
        if rc != 0: GLib.idle_add(self.on_fail, rc)
        else: GLib.idle_add(self.refresh_pending)
    def on_fail(self, rc):
        if rc in (126, 127): self.last_line = "Authorization cancelled or unavailable (rc=%d)" % rc
        else: self.error = f"apt exited with code {rc}"; notify(self.error)
        return False

# ------------------------------------------------------------------ drawing
def draw_orb(c, cx, cy, R, state, t, prog, count):
    col = COL[state]
    g = cairo.RadialGradient(cx, cy, R * .4, cx, cy, R * 1.5)
    a = .25 + .15 * math.sin(t * 3)
    g.add_color_stop_rgba(0, *col, a); g.add_color_stop_rgba(1, *col, 0)
    c.set_source(g); c.arc(cx, cy, R * 1.5, 0, 6.283); c.fill()
    c.arc(cx, cy, R, 0, 6.283); c.set_source_rgba(.06, .07, .11, .96); c.fill()
    lw = max(2, R * .16); c.set_line_width(lw); c.set_line_cap(cairo.LINE_CAP_ROUND)
    c.set_source_rgba(1, 1, 1, .12); c.arc(cx, cy, R * .84, 0, 6.283); c.stroke()
    c.set_source_rgb(*col)
    if state == "busy":
        n = 60; end = max(prog, .04)                                   # pastel conic progress ring, hues flow around it
        for i in range(int(n * end)):
            a0 = -math.pi / 2 + 6.283 * i / n; r_, g_, b_ = pastel(i / n * .9 + t * .25, .5)
            c.set_source_rgb(r_, g_, b_); c.arc(cx, cy, R * .84, a0, a0 + 6.283 / n * 1.15); c.stroke()
        c.set_source_rgb(*col)
        for k in range(6):                                             # packages falling into the core
            u = (t * .55 + k / 6) % 1.0; a2 = k * 1.047 + t * .2
            rr = R * (.72 - .55 * u); px, py = cx + rr * math.cos(a2), cy + rr * math.sin(a2)
            c.set_source_rgba(*pastel(k / 6 + t * .2, .35), math.sin(math.pi * u)); s = R * .11 * (1 - .5 * u)
            c.rectangle(px - s, py - s, 2 * s, 2 * s); c.fill()
        for k in range(8):                                             # rotating gear teeth
            a3 = t * 1.6 + k * .785; c.set_source_rgba(*pastel(k / 8 + t * .3, .5), .95)
            c.arc(cx + R * .98 * math.cos(a3), cy + R * .98 * math.sin(a3), R * .06, 0, 6.283); c.fill()
    elif state == "checking":
        c.arc(cx, cy, R * .84, t * 4, t * 4 + 1.8); c.stroke()
        for k in range(3):
            c.set_source_rgba(1, 1, 1, .4 + .6 * max(0, math.sin(t * 6 - k))); c.arc(cx - R * .3 + k * R * .3, cy, R * .09, 0, 6.283); c.fill()
    elif state == "updates":
        pulse = (t * .8) % 1.0
        c.set_source_rgba(*col, (1 - pulse) * .8); c.set_line_width(lw * .6); c.arc(cx, cy, R * (.85 + .5 * pulse), 0, 6.283); c.stroke()
        c.set_source_rgb(*col); c.set_line_width(lw); c.arc(cx, cy, R * .84, 0, 6.283); c.stroke()
        by = abs(math.sin(t * 3)) * R * .16; c.set_line_width(max(2, R * .2))
        c.move_to(cx, cy - R * .42 + by); c.line_to(cx, cy + R * .3 + by)
        c.move_to(cx - R * .3, cy + by); c.line_to(cx, cy + R * .32 + by); c.line_to(cx + R * .3, cy + by); c.stroke()
    elif state == "uptodate":
        pulse = (t * .5) % 1.0
        c.set_source_rgba(*col, (1 - pulse) * .6); c.set_line_width(lw * .6); c.arc(cx, cy, R * (.85 + .5 * pulse), 0, 6.283); c.stroke()
        c.set_source_rgb(*col); c.set_line_width(lw); c.arc(cx, cy, R * .84, 0, 6.283); c.stroke()
        c.set_line_width(max(2, R * .2)); c.move_to(cx - R * .36, cy + R * .02); c.line_to(cx - R * .1, cy + R * .3); c.line_to(cx + R * .38, cy - R * .25); c.stroke()
    elif state == "reboot":
        c.arc(cx, cy, R * .84, t * 1.5, t * 1.5 + 4.6); c.stroke()
        c.set_source_rgb(*col); c.set_line_width(max(2, R * .18)); c.arc(cx, cy, R * .38, -1.2, 3.6); c.stroke()
    else:
        c.set_line_width(max(2, R * .2)); c.move_to(cx - R * .3, cy - R * .3); c.line_to(cx + R * .3, cy + R * .3)
        c.move_to(cx + R * .3, cy - R * .3); c.line_to(cx - R * .3, cy + R * .3); c.stroke()

def draw_icon(c, W, H, m, t):
    st = m.state(); col = COL[st]
    c.set_operator(cairo.OPERATOR_SOURCE); c.set_source_rgba(0, 0, 0, 0); c.paint(); c.set_operator(cairo.OPERATOR_OVER)
    rrect(c, 1, H * .08, W - 2, H * .84, H * .32); c.set_source_rgba(.07, .08, .12, .92); c.fill_preserve()
    c.set_source_rgba(*col, .9); c.set_line_width(max(1.5, H * .05)); c.stroke()
    c.save(); rrect(c, 1, H * .08, W - 2, H * .84, H * .32); c.clip()
    for k in range(5):                                             # drifting pastel bubbles
        u = (t * (.12 + .1 * _pr(k, 1)) + _pr(k, 2)) % 1.0; px = W * (1 - u); py = H * (.25 + .5 * _pr(k, 3)) + 3 * math.sin(t * 2 + k)
        rr, gg, bb = pastel(_pr(k, 4) + t * .05, .35); c.set_source_rgba(rr, gg, bb, .22); c.arc(px, py, H * (.06 + .06 * _pr(k, 5)), 0, 6.283); c.fill()
    su = (t / 3.2) % 1.0
    if su < .5:
        sx = -H + (W + 2 * H) * su / .5; sg = cairo.LinearGradient(sx, 0, sx + H, 0)
        sg.add_color_stop_rgba(0, 1, 1, 1, 0); sg.add_color_stop_rgba(.5, 1, 1, 1, .3); sg.add_color_stop_rgba(1, 1, 1, 1, 0)
        c.set_source(sg); c.paint()
    sparkles(c, H * .1, H * .1, W - H * .2, H * .8, t, n=5, col=col, size=H * .13); c.restore()
    draw_orb(c, H * .5 + 1, H / 2, H * .36, st, t, m.progress(), len(m.pending))
    txt = {"busy": f"{int(m.progress() * 100)}%", "updates": str(len(m.pending)), "uptodate": "OK",
           "checking": "...", "reboot": "REBOOT", "error": "ERR"}[st]
    c.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    fs = H * (.36 if len(txt) <= 3 else .19); c.set_font_size(fs)
    ex = c.text_extents(txt); area = W - H * .95
    c.move_to(H * .95 + area / 2 - ex.width / 2 - ex.x_bearing, H / 2 - ex.height / 2 - ex.y_bearing)
    c.move_to(H * .95 + area / 2 - ex.width / 2 - ex.x_bearing, H / 2 - ex.height / 2 - ex.y_bearing); c.text_path(txt)
    tg = cairo.LinearGradient(H * .95, 0, W, 0); tg.add_color_stop_rgb(0, *pastel(t * .15, .25)); tg.add_color_stop_rgb(1, *pastel(t * .15 + .3, .35))
    c.set_source(tg); c.fill()

TITLES = {"uptodate": "System is up to date", "updates": "Updates available", "checking": "Checking for updates…",
          "busy": "Installing / updating…", "reboot": "Restart required", "error": "Something went wrong"}

def draw_panel(c, W, H, m, t, rows=8, born=0):
    st = m.state(); col = COL[st]
    rrect(c, 3, 3, W - 6, H - 6, 20); bg = cairo.LinearGradient(0, 0, 0, H)
    bg.add_color_stop_rgba(0, .10, .12, .17, .98); bg.add_color_stop_rgba(1, .05, .06, .09, .98)
    c.set_source(bg); c.fill_preserve(); c.set_source_rgba(1, 1, 1, .14); c.set_line_width(1.5); c.stroke()
    c.save(); rrect(c, 3, 3, W - 6, H - 6, 20); c.clip(); aurora(c, W, H, t); sparkles(c, 10, 10, W - 20, 70, t, n=8, col=col, size=6)
    if m.done_at and time.time() - m.done_at < 6 and not m.busy: confetti(c, W, H, time.time() - m.done_at)
    c.restore()
    draw_orb(c, 52, 52, 30, st, t, m.progress(), len(m.pending))
    c.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD); c.set_font_size(19)
    c.move_to(96, 42); c.text_path(TITLES[st]); tg = cairo.LinearGradient(96, 0, 380, 0)
    tg.add_color_stop_rgb(0, *pastel(t * .1, .25)); tg.add_color_stop_rgb(1, *pastel(t * .1 + .35, .4)); c.set_source(tg); c.fill()
    c.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL); c.set_font_size(12)
    c.set_source_rgba(.8, .85, .95, .9)
    sec = sum(1 for p in m.pending if p[3])
    sub = {"busy": f"{sum(1 for v in m.stages.values() if v == 'done')} of {m.total} packages — {m.current or '…'}",
           "updates": f"{len(m.pending)} package(s) to upgrade" + (f", {sec} security" if sec else ""),
           "uptodate": "Nothing to upgrade.", "checking": "Reading package lists…",
           "reboot": "A restart is needed to finish updates.", "error": m.error or ""}[st]
    c.move_to(96, 62); c.show_text(sub[:70])
    if st == "busy" or m.progress() > 0:
        rrect(c, 96, 72, W - 122, 8, 4); c.set_source_rgba(1, 1, 1, .1); c.fill()
        pw = max(8, (W - 122) * m.progress()); rrect(c, 96, 72, pw, 8, 4)
        pg = cairo.LinearGradient(96, 0, 96 + pw, 0); pg.add_color_stop_rgb(0, *pastel(t * .2, .45)); pg.add_color_stop_rgb(1, *pastel(t * .2 + .4, .55)); c.set_source(pg); c.fill()
        hx = 96 + ((t * .8) % 1.0) * pw; hg = cairo.RadialGradient(hx, 76, 0, hx, 76, 16); hg.add_color_stop_rgba(0, 1, 1, 1, .8); hg.add_color_stop_rgba(1, 1, 1, 1, 0)
        c.save(); rrect(c, 96, 72, pw, 8, 4); c.clip(); c.set_source(hg); c.paint(); c.restore()
    # package rows
    names = m.order if (m.busy and m.order) else [p[0] for p in m.pending]
    y0 = 104; c.set_font_size(12)
    for i, n in enumerate(names[:rows]):
        stage = m.stages.get(n, "queued") if m.busy else "queued"
        appear = min(1, max(0, (time.time() - born) * 3 - i * .25)) if born else 1
        yy = y0 + i * 24; xo = (1 - appear) * 40
        rrect(c, 20 + xo, yy - 14, W - 40, 20, 8); c.set_source_rgba(1, 1, 1, .05 * appear + (.06 if n == m.current and m.busy else 0)); c.fill()
        sc = STAGE_COL[stage]; pulse = .6 + .4 * math.sin(t * 8) if stage in ("unpacking", "configuring") else 1
        c.set_source_rgba(*sc, pulse * appear); c.arc(32 + xo, yy - 4, 4.5, 0, 6.283); c.fill()
        c.set_source_rgba(.95, .97, 1, appear); c.move_to(44 + xo, yy); c.show_text(n[:34])
        info = stage if m.busy else next((f"{p[1][:14]} -> {p[2][:14]}" for p in m.pending if p[0] == n), "")
        c.set_source_rgba(.7, .76, .88, .9 * appear); ex = c.text_extents(info)
        c.move_to(W - 30 - ex.width + xo, yy); c.show_text(info)
    if len(names) > rows:
        c.set_source_rgba(.7, .76, .88, .8); c.move_to(24, y0 + rows * 24 + 4); c.show_text(f"+ {len(names) - rows} more…")
    if m.last_line and (m.busy or m.runner):
        c.set_source_rgba(.6, .8, 1, .9); c.set_font_size(11); c.move_to(20, H - 16); c.show_text(m.last_line[:80])

# ------------------------------------------------------------------ UI
class MonitorWindow(Gtk.Window):
    W, H = 560, 380
    def __init__(self, tray):
        super().__init__(title="Update Monitor"); self.tray = tray; self.born = time.time()
        self.set_default_size(self.W, self.H + 52); self.set_resizable(False); self.set_position(Gtk.WindowPosition.CENTER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); self.add(box)
        self.da = Gtk.DrawingArea(); self.da.set_size_request(self.W, self.H); box.pack_start(self.da, True, True, 0)
        self.da.connect("draw", lambda w, c: draw_panel(c, self.W, self.H, tray.m, tray.t, rows=10, born=self.born))
        row = Gtk.Box(spacing=8); row.set_border_width(8); box.pack_start(row, False, False, 0)
        for label, cb in (("Check for updates", tray.do_update), ("Install updates", tray.do_upgrade), ("Close", lambda *a: self.destroy())):
            b = Gtk.Button(label=label); b.connect("clicked", cb); row.pack_start(b, True, True, 0)
        self.alive = True; self.connect("destroy", self.on_destroy); GLib.timeout_add(50, self.tick); self.show_all()
    def on_destroy(self, *a): self.alive = False; self.tray.win = None
    def tick(self):
        if not self.alive: return False
        self.da.queue_draw(); return True

class Popup(Gtk.Window):
    W, H = 380, 250
    def __init__(self, tray):
        super().__init__(type=Gtk.WindowType.POPUP); self.tray = tray
        self.set_app_paintable(True); self.set_decorated(False); self.set_keep_above(True)
        v = self.get_screen().get_rgba_visual()
        if v: self.set_visual(v)
        self.set_default_size(self.W, self.H); self.da = Gtk.DrawingArea(); self.add(self.da)
        self.da.connect("draw", self.on_draw); self.shown_at = 0; self.closing = None; self.tid = None
    def open(self, x, y):
        self.move(max(0, x - self.W // 2), max(0, y - self.H - 8)); self.shown_at = time.time(); self.closing = None
        self.show_all()
        if not self.tid: self.tid = GLib.timeout_add(33, self.tick)
    def close_anim(self):
        if self.closing is None: self.closing = time.time()
    def tick(self):
        self.da.queue_draw()
        if self.closing and time.time() - self.closing > .18: self.hide(); self.tid = None; return False
        return True
    def on_draw(self, w, c):
        c.set_operator(cairo.OPERATOR_SOURCE); c.set_source_rgba(0, 0, 0, 0); c.paint(); c.set_operator(cairo.OPERATOR_OVER)
        now = time.time(); x = min(1, (now - self.shown_at) / .35); k = 1 + 2.70158 * (x - 1) ** 3 + 1.70158 * (x - 1) ** 2
        if self.closing: k = max(0, 1 - (now - self.closing) / .18) * k
        c.translate(self.W / 2, self.H - 4); c.scale(max(k, .01), max(k, .01)); c.translate(-self.W / 2, -(self.H - 4))
        draw_panel(c, self.W, self.H, self.tray.m, self.tray.t, rows=5, born=self.shown_at)

class Tray:
    def __init__(self):
        self.m = Model(); self.t = 0.0; self.last = time.time(); self.win = None
        self.pop = Popup(self); self.out_since = None; self.watch_id = None
        self.plug = self.da = None; self.icon = None
        if not self.dock(): self.fallback()
        GLib.timeout_add(1000, self.m.poll); GLib.timeout_add(60, self.render)
        GLib.timeout_add_seconds(1800, lambda: (self.m.refresh_pending(), True)[1])

    def dock(self):
        try:
            from Xlib import X, display, protocol
            d = display.Display(); owner = d.get_selection_owner(d.intern_atom("_NET_SYSTEM_TRAY_S0"))
            if owner == X.NONE: return False
            plug = Gtk.Plug.new(0); plug.set_app_paintable(True)
            vis = plug.get_screen().get_rgba_visual()
            if vis: plug.set_visual(vis)
            da = Gtk.DrawingArea(); da.set_size_request(58, 32)
            da.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)
            da.connect("draw", lambda w, c: (draw_icon(c, w.get_allocated_width(), w.get_allocated_height(), self.m, self.t), False)[1])
            da.connect("enter-notify-event", self.on_enter); da.connect("button-press-event", self.on_click)
            da.connect("size-allocate", lambda w, r: r.height > 8 and abs(r.width - int(r.height * 1.8)) > 1 and w.set_size_request(int(r.height * 1.8), r.height))
            plug.add(da); plug.show_all(); self.da = da; self.plug = plug
            ev = protocol.event.ClientMessage(window=owner, client_type=d.intern_atom("_NET_SYSTEM_TRAY_OPCODE"),
                                              data=(32, [X.CurrentTime, 0, plug.get_id(), 0, 0]))
            owner.send_event(ev, event_mask=X.NoEventMask); d.flush()
            GLib.timeout_add(2500, self.check_embedded); return True
        except Exception as e:
            print("dock failed:", e, flush=True); return False
    def check_embedded(self):
        if self.plug and not self.plug.get_embedded(): self.plug.destroy(); self.plug = None; self.fallback()
        return False
    def fallback(self):
        from gi.repository import GdkPixbuf
        self.icon = Gtk.StatusIcon(); self.icon.set_visible(True); self.icon.connect("activate", lambda i: self.open_win())
        self.icon.set_has_tooltip(True); self.icon.connect("query-tooltip", lambda *a: (self.on_enter(), False)[1])
        self.icon.connect("popup-menu", lambda i, b, tm: self.menu().popup(None, None, Gtk.StatusIcon.position_menu, i, b, tm))
        self.pb = GdkPixbuf
    def render(self):
        now = time.time(); self.t += now - self.last; self.last = now
        if self.plug: self.da.queue_draw()
        elif self.icon:
            S = 64; W = int(S * 1.8); surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, S); draw_icon(cairo.Context(surf), W, S, self.m, self.t)
            self.icon.set_from_pixbuf(Gdk.pixbuf_get_from_surface(surf, 0, 0, W, S))
        return True
    def geom(self):
        if self.plug:
            win = self.da.get_window()
            if not win: return None
            _, x, y = win.get_origin()
            return type("R", (), dict(x=x, y=y, width=self.da.get_allocated_width(), height=self.da.get_allocated_height()))
        ok, s, a, o = self.icon.get_geometry(); return a if ok else None
    def on_enter(self, *a):
        g = self.geom()
        if g and (not self.pop.get_visible() or self.pop.closing):
            self.pop.open(g.x + g.width // 2, g.y)
            if not self.watch_id: self.watch_id = GLib.timeout_add(120, self.watch)
        self.out_since = None; return False
    def watch(self):
        g = self.geom(); _, x, y = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
        inside = g and g.x - 4 <= x <= g.x + g.width + 4 and g.y - 4 <= y <= g.y + g.height + 4
        if inside: self.out_since = None; return True
        if self.out_since is None: self.out_since = time.time()
        if time.time() - self.out_since > .35: self.pop.close_anim(); self.watch_id = None; return False
        return True
    def on_click(self, w, ev):
        if ev.button == 3: self.menu().popup_at_pointer(ev)
        else: self.open_win()
        return True
    def open_win(self):
        self.pop.close_anim()
        if self.win: self.win.present()
        else: self.win = MonitorWindow(self)
    def menu(self):
        mn = Gtk.Menu()
        for label, cb in (("Open Update Monitor", lambda w: self.open_win()), ("Check for updates (apt update)", self.do_update),
                          ("Install updates (apt upgrade)", self.do_upgrade), ("Install incl. new dependencies (full-upgrade)", self.do_full),
                          ("Re-scan pending list", lambda w: self.m.refresh_pending())):
            i = Gtk.MenuItem(label=label); i.connect("activate", cb); mn.append(i)
        mn.append(Gtk.SeparatorMenuItem()); q = Gtk.MenuItem(label="Quit"); q.connect("activate", lambda w: Gtk.main_quit()); mn.append(q)
        mn.show_all(); return mn
    def do_update(self, *a):
        self.m.run_root(["update"], "Refreshing package lists…")
    def confirm(self, what):
        n = len(self.m.pending)
        d = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
                              text=f"{what} {n} package{'s' if n != 1 else ''}?")
        d.format_secondary_text("You will be asked for your password. Keep the laptop on power during the install.")
        d.set_keep_above(True); d.set_position(Gtk.WindowPosition.CENTER); r = d.run(); d.destroy(); return r == Gtk.ResponseType.OK
    def do_upgrade(self, *a):
        if self.confirm("Install updates for"): self.m.run_root(["upgrade"], "Installing updates…")
    def do_full(self, *a):
        if self.confirm("Full-upgrade"): self.m.run_root(["full-upgrade"], "Installing updates (full)…")

def single_instance():
    """Only one tray icon per user (autostart + a manual start would otherwise draw two)."""
    import fcntl
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    f = open(os.path.join(LOG_DIR, "lock"), "w")
    try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: return None
    return f

if __name__ == "__main__":
    _lock = single_instance()
    if _lock is None: print("update-tray is already running", flush=True); raise SystemExit(0)
    Tray(); Gtk.main()
