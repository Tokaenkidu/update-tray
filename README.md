# 🌈 Update Tray — the system updater that is actually fun to watch

**Made by [EpicWebDesignStudio](https://www.epicwebdesignstudio.com)** · An animated panel-tray icon for Debian, Ubuntu and Linux Mint (MATE / Cinnamon / XFCE on X11).

It shows how many updates are waiting, and when an update runs it comes alive: every package being *unpacked → configured* is animated in a little aurora of pastel colours, with confetti when it's done.

No more staring at a frozen progress bar wondering "is it stuck?".

## ✨ What it does

- **Pending updates at a glance** — a wide tray icon with the number of upgradable packages (security updates counted separately).
- **Watches *any* apt/dpkg session** — Update Manager, a terminal `apt upgrade`, unattended upgrades: it follows them via `/proc` and `/var/log/dpkg.log`, so you always see what the system is doing right now.
- **Lively states** — *updates available · busy · checking · up to date · reboot required · error*, each with its own animation. Resident daemons (`packagekitd`, `unattended-upgrade-shutdown`) are not mistaken for real work.
- **Hover card + monitor window** — hover for a quick summary, click for the live package-by-package monitor.
- **Run updates from the tray** — `apt update` / `upgrade` / `full-upgrade` through **polkit (`pkexec`)** with a confirmation dialog. It **never** upgrades anything on its own: you click, you confirm, you type your password. Your own config files are kept (`--force-confold`).
- **One icon only** — starting it twice (autostart + manual) does not create two icons.
- **Falls back gracefully** to a normal square tray icon if the panel has no wide-icon support, and to a visible terminal if no password prompt appears.

## 📦 Install

```bash
sudo dpkg -i update-tray_1.1.1_all.deb
sudo apt -f install        # only if dpkg reports missing dependencies
update-tray &              # or log out/in: it autostarts
```

Dependencies: `python3`, `python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-3.0`, `python3-xlib`, `policykit-1` (pkexec).
Remove: `sudo apt remove update-tray`.

## 🧪 Tested

`tests/test_update_tray.py` covers `apt list` parsing, dpkg-log stage tracking, state priorities, "resident daemons are not busy", the single-instance lock, terminal fallback commands and drawing of every icon state. Run it with `python3 tests/test_update_tray.py` (needs an X display).

## 🛠️ Run from source

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-xlib policykit-1
python3 src/update_tray.py
```

## ⚠️ Honest notes

- Needs an **X11 session** with a system tray (MATE, Cinnamon, XFCE, …). On pure Wayland panels the XEmbed tray icon is not available.
- Young project. If *Install updates* ever does nothing, look at `~/.cache/update-tray/update-tray.log` — and please open an issue!

## 🙏 Thanks

A huge **thank you** to everyone who makes this possible and makes Linux a joy:

- the **Linux Mint, Debian and Ubuntu** communities,
- the **GTK / PyGObject / Cairo / python-xlib / polkit** maintainers,
- everybody who tests, reports bugs, shares screenshots and tells a friend about it.

If you enjoy it: ⭐ **star the repo, share it with a fellow Linux user, and tell us what you'd like to see next.** Pull requests, ideas and translations are very welcome.

## 📄 License

MIT — © 2026 **EpicWebDesignStudio**. Free to use, copy, modify and share.

---
Made with ❤️ by **EpicWebDesignStudio** (Tokaenkidu)
