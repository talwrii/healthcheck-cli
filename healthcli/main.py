#!/usr/bin/env python3
"""
hccli - simple local healthcheck CLI
Dead man's switch for cron jobs and scheduled tasks.
"""
import json
import subprocess
import sys
import time
import re
from pathlib import Path
from datetime import datetime

CONFIG_DIR = Path.home() / ".config" / "hccli"
CHECKS_FILE = CONFIG_DIR / "checks.json"


def load_checks():
    if not CHECKS_FILE.exists():
        return {}
    with open(CHECKS_FILE) as f:
        return json.load(f)


def save_checks(checks):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CHECKS_FILE, "w") as f:
        json.dump(checks, f, indent=2)


def parse_duration(s):
    """Parse '25h', '1d', '30m', '1d12h' etc into seconds"""
    total = 0
    for match in re.finditer(r"(\d+)\s*([smhdw])", s.lower()):
        val = int(match.group(1))
        unit = match.group(2)
        if unit == "s":
            total += val
        elif unit == "m":
            total += val * 60
        elif unit == "h":
            total += val * 3600
        elif unit == "d":
            total += val * 86400
        elif unit == "w":
            total += val * 604800
    if total == 0:
        try:
            total = int(s) * 3600
        except ValueError:
            print(f"Cannot parse duration: {s}", file=sys.stderr)
            print("Examples: 30m, 1h, 25h, 1d, 1d12h, 1w", file=sys.stderr)
            sys.exit(1)
    return total


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        if m:
            return f"{h}h{m}m"
        return f"{h}h"
    else:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        if h:
            return f"{d}d{h}h"
        return f"{d}d"


def format_ago(epoch):
    if epoch is None:
        return "never"
    diff = int(time.time() - epoch)
    if diff < 0:
        return "just now"
    return format_duration(diff) + " ago"


def check_status(check):
    """Returns (ok, message)"""
    last_ok = check.get("last_ok")
    last_fail = check.get("last_fail")
    every = check.get("every")

    if last_fail and (not last_ok or last_fail > last_ok):
        msg = check.get("fail_msg", "failed")
        return False, f"FAILED: {msg}"

    if last_ok is None:
        return False, "never run"

    age = time.time() - last_ok
    if age > every:
        overdue = age - every
        return False, f"OVERDUE {format_duration(int(overdue))}"

    return True, format_ago(check.get("service_ran", last_ok))


def cmd_add(args):
    every = None
    name = None
    sdtimer = None
    command = []
    i = 0
    while i < len(args):
        if args[i] == "--every" and i + 1 < len(args):
            every = parse_duration(args[i + 1])
            i += 2
        elif args[i] == "--name" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
        elif args[i] == "--sdtimer" and i + 1 < len(args):
            sdtimer = args[i + 1]
            i += 2
        else:
            command.append(args[i])
            i += 1

    if not command and not sdtimer:
        print("Usage: hccli add --every <duration> [--name <n>] <command> [args...]", file=sys.stderr)
        print("       hccli add --every <duration> --sdtimer <service> [--name <n>]", file=sys.stderr)
        sys.exit(1)

    if every is None:
        print("Missing --every", file=sys.stderr)
        sys.exit(1)

    if sdtimer:
        if name is None:
            name = sdtimer
        checks = load_checks()
        checks[name] = {
            "sdtimer": sdtimer,
            "every": every,
            "last_run": None,
            "last_ok": None,
            "last_fail": None,
            "fail_msg": None,
            "created": time.time(),
        }
        save_checks(checks)
        print(f"✓ Added '{name}' (every {format_duration(every)}): systemd timer {sdtimer}")
    else:
        if name is None:
            name = Path(command[0]).name
        checks = load_checks()
        checks[name] = {
            "command": command,
            "every": every,
            "last_run": None,
            "last_ok": None,
            "last_fail": None,
            "fail_msg": None,
            "created": time.time(),
        }
        save_checks(checks)
        print(f"✓ Added '{name}' (every {format_duration(every)}): {' '.join(command)}")


def cmd_rm(args):
    if len(args) < 1:
        print("Usage: hccli rm <n>", file=sys.stderr)
        sys.exit(1)

    name = args[0]
    checks = load_checks()
    if name not in checks:
        print(f"Unknown check: {name}", file=sys.stderr)
        sys.exit(1)

    del checks[name]
    save_checks(checks)
    print(f"✓ Removed '{name}'")


def run_check(name, check):
    """Run a single check command, return (ok, message)"""
    now = time.time()
    check["last_run"] = now

    # Systemd timer check
    sdtimer = check.get("sdtimer")
    if sdtimer:
        return run_sdtimer_check(name, check, sdtimer, now)

    command = check["command"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            check["last_ok"] = now
            check["last_fail"] = None
            check["fail_msg"] = None
            return True, "ok"
        else:
            check["last_fail"] = now
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            msg = stderr or stdout or f"exit {result.returncode}"
            if len(msg) > 200:
                msg = msg[:200] + "..."
            check["fail_msg"] = msg
            return False, msg
    except FileNotFoundError:
        check["last_fail"] = now
        check["fail_msg"] = f"command not found: {command[0]}"
        return False, check["fail_msg"]
    except subprocess.TimeoutExpired:
        check["last_fail"] = now
        check["fail_msg"] = "timeout (300s)"
        return False, "timeout (300s)"
    except Exception as e:
        check["last_fail"] = now
        check["fail_msg"] = str(e)
        return False, str(e)


def run_sdtimer_check(name, check, service, now):
    """Check a systemd user service ran successfully and recently"""
    every = check["every"]

    try:
        # Get result
        r = subprocess.run(
            ["systemctl", "--user", "show", "-p", "Result", "--value", f"{service}.service"],
            capture_output=True, text=True,
        )
        result = r.stdout.strip()

        # Get last finish time
        r = subprocess.run(
            ["systemctl", "--user", "show", "-p", "ExecMainExitTimestamp", "--value", f"{service}.service"],
            capture_output=True, text=True,
        )
        timestamp_str = r.stdout.strip()

        if not timestamp_str:
            check["last_fail"] = now
            check["fail_msg"] = "never run"
            return False, "never run"

        # Parse timestamp
        r = subprocess.run(
            ["date", "-d", timestamp_str, "+%s"],
            capture_output=True, text=True,
        )
        epoch = int(r.stdout.strip())
        age = now - epoch

        if result != "success":
            check["last_fail"] = now
            check["fail_msg"] = f"result={result}"
            return False, f"result={result}"

        if age > every:
            check["last_fail"] = now
            check["fail_msg"] = f"last run {format_duration(int(age))} ago"
            return False, f"last run {format_duration(int(age))} ago"

        check["last_ok"] = now
        check["last_fail"] = None
        check["fail_msg"] = None
        check["service_ran"] = epoch
        return True, f"ok (ran {format_ago(epoch)})"

    except Exception as e:
        check["last_fail"] = now
        check["fail_msg"] = str(e)
        return False, str(e)


def cmd_run(args):
    force = "--force" in args
    names = [a for a in args if not a.startswith("--")]

    checks = load_checks()
    if not checks:
        print("No checks configured.")
        return

    now = time.time()
    ran = 0

    for name, check in sorted(checks.items()):
        if names and name not in names:
            continue

        if not force:
            last_run = check.get("last_run")
            if last_run and (now - last_run) < check["every"]:
                continue

        ran += 1
        ok, msg = run_check(name, check)
        icon = "✅" if ok else "❌"
        print(f"{icon} {name}: {msg}")

    save_checks(checks)

    if ran == 0 and not names:
        print("No checks due. Use --force to run anyway.")


def cmd_status(args):
    oneline = "--oneline" in args
    quiet = "--quiet" in args or "-q" in args

    checks = load_checks()
    if not checks:
        if not quiet:
            print("No checks configured. Add one: hccli add --every <duration> <command>")
        return

    ok_count = 0
    fail_count = 0
    fail_names = []

    for name, check in sorted(checks.items()):
        ok, msg = check_status(check)
        if ok:
            ok_count += 1
        else:
            fail_count += 1
            fail_names.append(name)

        if not oneline and not quiet:
            icon = "✅" if ok else "❌"
            every = format_duration(check["every"])
            if "sdtimer" in check:
                src = f"[sdtimer: {check['sdtimer']}]"
            else:
                src = f"[{' '.join(check['command'])}]"
            print(f"{icon} {name:<20} {msg:<25} (every {every}) {src}")

    if oneline:
        total = ok_count + fail_count
        if fail_count == 0:
            print(f"✅ {ok_count}/{total}")
        else:
            print(f"❌ {', '.join(fail_names)}")

    if fail_count > 0:
        sys.exit(1)


def cmd_list(args):
    checks = load_checks()
    if not checks:
        print("No checks configured.")
        return
    for name, check in sorted(checks.items()):
        every = format_duration(check["every"])
        if "sdtimer" in check:
            src = f"sdtimer: {check['sdtimer']}"
        else:
            src = " ".join(check["command"])
        created = datetime.fromtimestamp(check["created"]).strftime("%Y-%m-%d")
        print(f"  {name:<20} every {every:<10} {src} (added {created})")


def cmd_edit(args):
    if len(args) < 1:
        print("Usage: hccli edit <n> [--every <duration>]", file=sys.stderr)
        sys.exit(1)

    name = args[0]
    checks = load_checks()
    if name not in checks:
        print(f"Unknown check: {name}", file=sys.stderr)
        sys.exit(1)

    i = 1
    while i < len(args):
        if args[i] == "--every" and i + 1 < len(args):
            checks[name]["every"] = parse_duration(args[i + 1])
            i += 2
        else:
            i += 1

    save_checks(checks)
    print(f"✓ Updated '{name}'")


def cmd_reset(args):
    if len(args) < 1:
        print("Usage: hccli reset <n>", file=sys.stderr)
        sys.exit(1)

    name = args[0]
    checks = load_checks()
    if name not in checks:
        print(f"Unknown check: {name}", file=sys.stderr)
        sys.exit(1)

    checks[name]["last_run"] = None
    checks[name]["last_ok"] = None
    checks[name]["last_fail"] = None
    checks[name]["fail_msg"] = None
    save_checks(checks)
    print(f"✓ Reset '{name}'")


def cmd_install(args):
    """Install systemd user timer to run hccli run"""
    import shutil

    every = "5m"
    i = 0
    while i < len(args):
        if args[i] == "--every" and i + 1 < len(args):
            every = args[i + 1]
            i += 2
        else:
            i += 1

    # Convert to systemd OnCalendar or OnUnitActiveSec format
    secs = parse_duration(every)
    if secs < 60:
        interval = f"{secs}s"
    elif secs < 3600:
        interval = f"{secs // 60}m"
    elif secs < 86400:
        interval = f"{secs // 3600}h"
    else:
        interval = f"{secs // 86400}d"

    hccli_path = shutil.which("hccli")
    if not hccli_path:
        hccli_path = sys.argv[0]

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)

    service = unit_dir / "hccli.service"
    timer = unit_dir / "hccli.timer"

    service.write_text(f"""[Unit]
Description=Run hccli healthchecks

[Service]
Type=oneshot
ExecStart={hccli_path} run
""")

    timer.write_text(f"""[Unit]
Description=Run hccli healthchecks every {interval}

[Timer]
OnBootSec=1m
OnUnitActiveSec={interval}
Persistent=true

[Install]
WantedBy=timers.target
""")

    subprocess.run(["systemctl", "--user", "daemon-reload"])
    subprocess.run(["systemctl", "--user", "enable", "--now", "hccli.timer"])

    print(f"✓ Installed systemd user timer (every {interval})")
    print(f"  Service: {service}")
    print(f"  Timer:   {timer}")
    print(f"  Command: {hccli_path} run")
    print()
    print("Check with: systemctl --user status hccli.timer")


def cmd_uninstall(args):
    """Remove systemd user timer"""
    subprocess.run(["systemctl", "--user", "disable", "--now", "hccli.timer"])

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    for f in ["hccli.service", "hccli.timer"]:
        p = unit_dir / f
        if p.exists():
            p.unlink()

    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print("✓ Uninstalled systemd user timer")


def show_help():
    print("hccli - simple local healthcheck CLI")
    print()
    print("Usage: hccli [command] [args]")
    print()
    print("Commands:")
    print("  (no command)                       Show status")
    print("  add --every <dur> [--name <n>] <cmd> [args]")
    print("  add --every <dur> --sdtimer <service> [--name <n>]")
    print("                                     Add a check")
    print("  rm <n>                             Remove a check")
    print("  run [name] [--force]               Run due checks (or all with --force)")
    print("  status                             Show all checks")
    print("  status --oneline                   Single line (for widgets)")
    print("  status --quiet                     Exit code only (0=ok, 1=fail)")
    print("  list                               List checks")
    print("  edit <n> --every <dur>             Update check interval")
    print("  reset <n>                          Clear run history")
    print("  install [--every <dur>]            Install systemd timer (default: 5m)")
    print("  uninstall                          Remove systemd timer")
    print()
    print("Durations: 30m, 1h, 25h, 1d, 1d12h, 1w")
    print()
    print("Examples:")
    print("  hccli add --every 25h backup-home")
    print("  hccli add --every 25h --sdtimer backup-home")
    print("  hccli add --every 5m false")
    print("  hccli add --every 1h --name web curl -sf https://example.com")
    print("  hccli run")
    print("  hccli status")
    print()
    print("Run all due checks every minute:")
    print("  * * * * * hccli run")


def main():
    if len(sys.argv) < 2:
        cmd_status([])
        return

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "add": cmd_add,
        "rm": cmd_rm,
        "remove": cmd_rm,
        "run": cmd_run,
        "status": cmd_status,
        "list": cmd_list,
        "ls": cmd_list,
        "edit": cmd_edit,
        "reset": cmd_reset,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "help": lambda a: show_help(),
        "--help": lambda a: show_help(),
        "-h": lambda a: show_help(),
    }

    if cmd in commands:
        commands[cmd](args)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        show_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
