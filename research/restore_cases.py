#!/usr/bin/env python3
"""Research harness for the fork Intellectualguy1/dikumud3-restore-research.

It drives a locally running DikuMUD3 (vme + mplex) over telnet and never edits a game
source file. It builds one damaged game state, then runs two recovery cases, each from a
fresh copy of that state:

  setup   healthy server (new characters type `save`: `quit` drops a guest unsaved) -> backup
          -> wrong zone name -> Alice logs in and saves the slimed box -> Bob earns a new item
          -> clean shutdown -> snapshot
  case1   put the zone name back and restart; restore no player file
  case2   put the zone name back; restore only Alice's inventory file from the backup;
          Alice opens the box and takes the item out; check again after a clean restart

Everything observed is written to OUT (default research-out/) and to the GitHub step
summary. Usage: restore_cases.py [all|setup|case1|case2|selftest]
"""
import itertools
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import time
import traceback
import hashlib

ROOT = os.path.abspath(os.environ.get("DM3_ROOT", os.getcwd()))
VME = os.path.join(ROOT, "vme")
BIN = os.path.join(VME, "bin")
LIB = os.path.join(VME, "lib")
ZONELIST = os.path.join(VME, "etc", "zonelist")
OUT = os.path.abspath(os.environ.get("OUT", os.path.join(ROOT, "research-out")))
PORT = int(os.environ.get("DM3_PORT", "4242"))
MOCK = os.environ.get("DM3_MOCK") == "1"

PASSWORDS = {"Papi": "Papi1234", "Alice": "Alice123", "Bob": "Bob12345"}
ZONE_GOOD, ZONE_BAD = "holiday20", "holiday2020"
BOX = "xmas_box@holiday20"      # the 2020 Christmas Gift box (a container)
BUN = "honeybun@udgaard"        # ordinary item Alice stores in the box
SPATULA = "moot_epsp@udgaard"   # ordinary item Bob earns after the backup (new fighters start with an axe)
MARKERS = itertools.count(1)

os.makedirs(OUT, exist_ok=True)
_SUMMARY = open(os.path.join(OUT, "summary.md"), "a", encoding="utf-8")
_GH = os.environ.get("GITHUB_STEP_SUMMARY")
RESULTS = {}
MISSING = {}    # boot tag -> number of "Could not open data file" lines in that boot's vme.log


def say(msg):
    print(msg, flush=True)


def summary(md):
    _SUMMARY.write(md + "\n")
    _SUMMARY.flush()
    if _GH:
        with open(_GH, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")


def block(title, text, limit=5000):
    text = (text or "").strip("\n")
    if len(text) > limit:
        text = text[: limit // 2] + "\n...[cut]...\n" + text[-limit // 2:]
    summary(f"\n**{title}**\n\n```text\n{text}\n```")
    say(f"::group::{title}\n{text}\n::endgroup::")


# --------------------------------------------------------------------------- telnet

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>78]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Timeout(Exception):
    pass


class Conn:
    def __init__(self, name, phase):
        self.name = name
        self.phase = phase
        self.sock = socket.create_connection(("127.0.0.1", PORT), timeout=15)
        self.sock.settimeout(None)
        self.rawbuf = b""
        self.text = ""
        self.pos = 0
        self.closed = False
        self.n = 0
        os.makedirs(os.path.join(OUT, "transcripts"), exist_ok=True)
        self.tf = open(os.path.join(OUT, "transcripts", f"{phase}-{name}.txt"), "a", encoding="utf-8")
        self.tf.write(f"=== connect {name} ({phase}) ===\n")

    # raw bytes -> telnet-free bytes (answers option requests with refusals)
    def _parse(self):
        b, out, i, n = self.rawbuf, bytearray(), 0, len(self.rawbuf)
        while i < n:
            c = b[i]
            if c != 255:
                out.append(c)
                i += 1
                continue
            if i + 1 >= n:
                break
            cmd = b[i + 1]
            if cmd == 255:
                out.append(255)
                i += 2
            elif cmd in (251, 252, 253, 254):
                if i + 2 >= n:
                    break
                opt = b[i + 2]
                try:
                    if cmd == 253:
                        self.sock.sendall(bytes([255, 252, opt]))
                    elif cmd == 251:
                        self.sock.sendall(bytes([255, 254, opt]))
                except OSError:
                    pass
                i += 3
            elif cmd == 250:
                j = b.find(bytes([255, 240]), i + 2)
                if j < 0:
                    break
                i = j + 2
            else:
                i += 2
        self.rawbuf = b[i:]
        return bytes(out)

    def pump(self, timeout=0.5):
        if self.closed:
            time.sleep(min(timeout, 0.2))
            return False
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return False
        try:
            data = self.sock.recv(65536)
        except OSError:
            data = b""
        if not data:
            self.closed = True
            self.tf.write("\n=== connection closed by server ===\n")
            self.tf.flush()
            return True
        self.rawbuf += data
        txt = ANSI.sub("", self._parse().decode("latin-1")).replace("\r", "")
        txt = CTRL.sub("", txt)
        self.text += txt
        self.tf.write(txt)
        self.tf.flush()
        return True

    def tail(self, n=1500):
        return self.text[-n:]

    def take(self):
        chunk = self.text[self.pos:]
        self.pos = len(self.text)
        return chunk

    def wait(self, pattern, timeout=60.0):
        end = time.time() + timeout
        rx = re.compile(pattern, re.I | re.S)
        while True:
            m = rx.search(self.text, self.pos)
            if m:
                chunk = self.text[self.pos:m.end()]
                self.pos = m.end()
                return chunk
            if self.closed:
                raise Timeout(f"{self.name}: connection closed while waiting for {pattern!r}\n--- tail ---\n{self.tail()}")
            left = end - time.time()
            if left <= 0:
                raise Timeout(f"{self.name}: no {pattern!r} within {timeout:.0f}s\n--- tail ---\n{self.tail()}")
            self.pump(min(0.5, left))

    def quiet(self, secs=2.0, limit=30.0):
        end, last = time.time() + limit, time.time()
        while time.time() < end and not self.closed:
            if self.pump(0.25):
                last = time.time()
            elif time.time() - last >= secs:
                break

    def send(self, line, secret=False):
        self.tf.write(f"\n>>> {'********' if secret else line}\n")
        self.tf.flush()
        try:
            self.sock.sendall(line.encode("latin-1") + b"\r\n")
        except OSError:
            self.closed = True

    def cmd(self, line, timeout=60.0):
        """Send a command, then a marker `say`; return everything the command printed."""
        # one counter for all connections: everybody in the room hears everybody's `say`
        mk = f"ZQ{next(MARKERS)}QZ"
        self.send(line)
        self.send(f"say {mk}")
        chunk = self.wait(re.escape(mk) + r"[^\n]*\n?", timeout)
        return re.sub(r"[^\n]*" + re.escape(mk) + r"[^\n]*\n?", "", chunk)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.closed = True
        self.tf.close()


OPEN = []


def settle(c, who):
    """After the password: dismiss menus until we are in the game, then prove it."""
    deadline = time.time() + 150
    while time.time() < deadline:
        c.quiet(3.0, 45)
        chunk = c.take()
        low = chunk.lower()
        if "throw the other copy out" in low:
            c.send("y")
        elif "make your choice" in low and "enter valhalla" in low:
            c.send("1")
        elif "wrong password" in low:
            raise Timeout(f"{who}: wrong password\n{chunk[-600:]}")
        else:
            break
    return c.cmd("look")


def creation(c, who):
    """Answer the character-creation menus (gender, profession, race, personality, newbie)."""
    unknown, empties, finished = 0, 0, False
    for _ in range(80):
        c.quiet(2.5, 45)
        chunk = c.take()
        low = chunk.lower()
        if not low.strip():
            empties += 1
            if finished or empties >= 8:    # ~20 s of silence before the first prompt is too long
                break
            continue
        empties = 0
        if finished and "make your choice" not in low:
            c.pos -= len(chunk)             # first game text after the last menu: leave it unread
            break
        if "throw the other copy out" in low:
            c.send("y")
        elif "make your choice" in low and "enter valhalla" in low:
            c.send("1")
            finished = True
        elif "make your choice" in low and "enter the game" in low:
            c.send("3")
            finished = True
        elif "gender" in low:
            c.send("1")
        elif "profession" in low:
            c.send("1")
        elif re.search(r"\brace\b", low):
            c.send("1")
        elif "personality" in low:
            nums = re.findall(r"select (\d)", low)
            c.send(nums[-1] if nums else "1")
        elif "press" in low and ("return" in low or "enter" in low):
            c.send("")
        else:
            unknown += 1
            say(f"[{who}] creation: unrecognised text:\n{chunk[-500:]}")
            if unknown >= 3:
                raise Timeout(f"{who}: stuck in character creation\n{chunk[-800:]}")
    return c.cmd("look")


def login(name, phase, new=False):
    c = Conn(name, phase)
    OPEN.append(c)
    c.wait(r"By what name do they call you\?", 240)
    c.send(name)
    if new:
        c.wait(r"Did I get that right, \w+ \(Y/N\)\?", 40)
        c.send("y")
        c.wait(r"Give me a new password for[^\n]*: ", 40)
        c.send(PASSWORDS[name], secret=True)
        c.wait(r"Please retype password: ", 40)
        c.send(PASSWORDS[name], secret=True)
        creation(c, name)
    else:
        c.wait(r"please enter your password: ", 40)
        c.send(PASSWORDS[name], secret=True)
        settle(c, name)
    return c


def leave(c):
    """quit and wait for the server to drop the connection (that is when the save happens)."""
    c.send("quit")
    end = time.time() + 40
    while not c.closed and time.time() < end:
        c.pump(0.5)
    c.quiet(0.5, 3)
    ok = c.closed
    c.close()
    return ok


def save_guest(c, phase):
    """A new character is a guest, and `quit` drops a guest without writing any file (run 2 of this
    harness showed it: the vme.log said "(GUEST) has entered the game" and no Alice file existed
    after the shutdown). `save` is what turns the guest into a saved player."""
    out = c.cmd("save")
    block(f"{phase} {c.name}: save", out)
    return out


# --------------------------------------------------------------------------- server

def read_log():
    path = os.path.join(BIN, "vme.log")
    try:
        with open(path, errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


class Server:
    def __init__(self, tag):
        self.tag = tag
        self.boot_seconds = None
        self.vp = self.mp = None

    def start(self):
        if MOCK:
            return self
        try:
            os.remove(os.path.join(BIN, "vme.log"))
        except OSError:
            pass
        self.vout = open(os.path.join(OUT, f"vme-stdout-{self.tag}.txt"), "w")
        t0 = time.time()
        self.vp = subprocess.Popen(["./vme"], cwd=BIN, stdout=self.vout, stderr=subprocess.STDOUT)
        end = time.time() + 900
        while time.time() < end:
            if self.vp.poll() is not None:
                raise RuntimeError(f"vme exited during boot ({self.vp.returncode}):\n{read_log()[-3000:]}")
            if "Entering game loop." in read_log():
                break
            time.sleep(1.0)
        else:
            raise Timeout(f"vme did not reach the game loop:\n{read_log()[-3000:]}")
        self.boot_seconds = round(time.time() - t0, 1)
        MISSING[self.tag] = sum(1 for ln in read_log().splitlines() if "Could not open data file" in ln)
        self.mout = open(os.path.join(OUT, f"mplex-{self.tag}.txt"), "w")
        self.mp = subprocess.Popen(["./mplex", "-p", str(PORT)], cwd=BIN, stdout=self.mout, stderr=subprocess.STDOUT)
        probe_end = time.time() + 120
        while True:
            try:
                p = Conn("probe", f"boot-{self.tag}")
                p.wait(r"By what name do they call you\?", 30)
                p.close()
                break
            except (Timeout, OSError):
                if time.time() > probe_end:
                    raise
                time.sleep(2)
        say(f"[{self.tag}] server ready after {self.boot_seconds}s")
        return self

    def stop(self, god):
        """Clean stop: the god types `shutdown`; the server saves everyone and exits."""
        if MOCK:
            god.send("shutdown")
            god.quiet(0.5, 3)
            return
        god.send("shutdown")
        try:
            self.vp.wait(timeout=240)
        except subprocess.TimeoutExpired:
            say(f"[{self.tag}] vme did not exit after shutdown; sending SIGTERM")
            self.vp.terminate()
            self.vp.wait(timeout=120)
        time.sleep(1.0)
        if self.mp.poll() is None:
            self.mp.terminate()
            try:
                self.mp.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.mp.kill()
        try:
            shutil.copy(os.path.join(BIN, "vme.log"), os.path.join(OUT, f"vme-{self.tag}.log"))
        except OSError as exc:
            say(f"[{self.tag}] could not copy vme.log: {exc}")
        for c in list(OPEN):
            c.close()
            OPEN.remove(c)


def kill_leftovers():
    for p in ("vme", "mplex"):
        subprocess.run(["pkill", "-x", p], check=False)


# --------------------------------------------------------------------------- files

PLY = os.path.join(LIB, "ply")


def ply(name, suffix=""):
    return os.path.join(PLY, name[0].lower(), name.lower() + suffix)


def digest(path):
    if not os.path.exists(path):
        return "missing"
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return f"{os.path.getsize(path)} bytes, sha256 {h.hexdigest()[:16]}"


def tokens(path, words):
    if not os.path.exists(path):
        return "file missing"
    data = open(path, "rb").read()
    return ", ".join(f"{w}={'yes' if w.encode() in data else 'no'}" for w in words)


def evidence(title, backup_dir):
    rows = []
    words = ["xmas_box", "holiday20", "slime", "basis", "honeybun", "udgaard", "moot_epsp"]
    for who in ("alice", "bob"):
        for suffix in ("", ".inv"):
            live = ply(who, suffix)
            back = os.path.join(backup_dir, who[0], who + suffix)
            rows.append(f"{who}{suffix}\n  live:   {digest(live)}\n          {tokens(live, words)}\n"
                        f"  backup: {digest(back)}\n          {tokens(back, words)}")
    block(title, "\n".join(rows))


def zonelist_text():
    return open(ZONELIST, encoding="latin-1").read()


def set_zone_name(old, new):
    txt = zonelist_text()
    new_txt, n = re.subn(rf"^{re.escape(old)}(\s)", rf"{new}\1", txt, count=1, flags=re.M)
    if n != 1:
        raise RuntimeError(f"zonelist has no line starting with {old!r}")
    with open(ZONELIST, "w", encoding="latin-1") as fh:
        fh.write(new_txt)
    line = [ln for ln in new_txt.splitlines() if ln.startswith(new + " ") or ln.startswith(new + "\t")][0]
    say(f"zonelist now: {line}")


def log_lines(*needles):
    return "\n".join(ln for ln in read_log().splitlines() if any(n in ln for n in needles))


def zone_lines(tag, *names):
    """The vme.log lines about the holiday zones, and how this boot compares with the T0 boot.
    This checkout lacks the data file of many zones listed in etc/zonelist; every boot says so."""
    mine = log_lines(*[f"Indexing {n} " for n in names], *[f"zone/{n}.data" for n in names])
    return (f"{mine}\n"
            f"'Could not open data file' lines in this boot: {MISSING.get(tag)}; in the T0 boot: {MISSING.get('t0')}")


SNAP = os.path.join(OUT, "snap")
BACKUP = os.path.join(OUT, "backup-ply")


def snapshot():
    if os.path.exists(SNAP):
        shutil.rmtree(SNAP)
    os.makedirs(SNAP)
    shutil.copytree(LIB, os.path.join(SNAP, "lib"))
    shutil.copy2(ZONELIST, os.path.join(SNAP, "zonelist"))


def restore_snapshot():
    kill_leftovers()
    shutil.rmtree(LIB)
    shutil.copytree(os.path.join(SNAP, "lib"), LIB)
    shutil.copy2(os.path.join(SNAP, "zonelist"), ZONELIST)


def starting_files(title):
    """Digest of the four player files and the holiday line of etc/zonelist: the proof that a case starts from the snapshot."""
    rows = [f"{w}{s}: {digest(ply(w, s))}" for w in ("alice", "bob") for s in ("", ".inv")]
    rows.append("etc/zonelist: " + " | ".join(ln.strip() for ln in zonelist_text().splitlines()
                                              if ln.startswith(("holiday20", "holiday2020"))))
    block(title, "\n".join(rows))


# --------------------------------------------------------------------------- scenario

def look_in_box(c):
    """Look inside the Christmas Gift box; try the usual spellings of the command."""
    out = ""
    for look in ("look in gift", "look into gift", "examine gift"):
        out = c.cmd(look)
        if "honey bun" in out.lower() or "empty" in out.lower():
            break
    return out


def need(text, word, what):
    """Stop early, with the real output, if a setup step did not do what the scenario needs."""
    if word.lower() not in (text or "").lower():
        raise RuntimeError(f"setup step failed: {what} (expected {word!r}) in:\n{text}")


def inv_flags(text):
    low = text.lower()
    return {
        "slime": "slimy remains" in low,
        "box": any("christmas" in ln and "gift" in ln and "slimy" not in ln for ln in low.splitlines()),
        "bun": "honey bun" in low,
        "spatula": "spatula" in low,
    }


def show(flags):
    """Plain words for what an inventory listing contained (a closed box does not list its contents)."""
    if flags is None:
        return "not run"
    names = {"box": "the original Christmas Gift", "slime": "the slimy remains of a Christmas Gift",
             "bun": "a honey bun in the pack", "spatula": "a spatula"}
    return ", ".join(v for k, v in names.items() if flags.get(k)) or "none of the four"


def box_check(c, label):
    """Open the Christmas Gift, look inside, take the honey bun out; return (inventory flags, bun was listed)."""
    block(f"{label}: Alice open gift", c.cmd("open gift"))
    look_in = look_in_box(c)
    block(f"{label}: Alice look in gift", look_in)
    got = c.cmd("get bun from gift")
    if "honey bun" not in got.lower() and "you get" not in got.lower():
        got += "\n--- retry without 'from' ---\n" + c.cmd("get bun gift")
    block(f"{label}: Alice get bun from gift", got)
    inv = c.cmd("inventory")
    block(f"{label}: Alice inventory after taking the item out", inv)
    return inv_flags(inv), "honey bun" in look_in.lower()


def setup():
    summary("\n## Setup: build the damaged game state")
    shutil.rmtree(OUT + "/transcripts", ignore_errors=True)

    # T0: healthy server
    srv = Server("t0").start()
    papi = login("Papi", "t0", new=True)
    alice = login("Alice", "t0", new=True)
    bob = login("Bob", "t0", new=True)
    block("T0 Papi after creation (look)", papi.cmd("look"))
    block("T0 Papi: goto alice", papi.cmd("goto alice"))
    out = papi.cmd(f"load {BOX}")
    block("T0 Papi: load box", out)
    need(out, "load", "god could not load the box")
    block("T0 Papi: give gift alice", papi.cmd("give gift alice"))
    inv = alice.cmd("inventory")
    block("T0 Alice inventory (box received)", inv)
    need(inv, "christmas", "Alice did not receive the Christmas Gift box")
    block("T0 Alice: open gift", alice.cmd("open gift"))
    block("T0 Papi: load bun", papi.cmd(f"load {BUN}"))
    block("T0 Papi: give bun alice", papi.cmd("give bun alice"))
    out = ""
    for put in ("put bun in gift", "put bun gift"):
        block(f"T0 Alice: {put}", alice.cmd(put))
        out = look_in_box(alice)
        block("T0 Alice: look in gift", out)
        if "honey bun" in out.lower():
            break
    need(out, "honey bun", "the bun is not inside the box")
    block("T0 Alice: close gift", alice.cmd("close gift"))
    t0_inv = alice.cmd("inventory")
    block("T0 Alice inventory before quitting", t0_inv)
    RESULTS["t0_alice"] = inv_flags(t0_inv)
    block("T0 Bob inventory", bob.cmd("inventory"))
    for c in (alice, bob, papi):
        save_guest(c, "T0")
    made = [ply("alice"), ply("alice", ".inv"), ply("bob"), ply("bob", ".inv"), ply("papi"), ply("papi", ".inv")]
    block("T0 player files on disk after the saves",
          "\n".join(f"{os.path.relpath(p, ROOT)}: {digest(p)}" for p in made))
    missing = [os.path.relpath(p, ROOT) for p in made if not os.path.exists(p)]
    if missing:
        raise RuntimeError("setup step failed: the saves wrote no file for: " + ", ".join(missing))
    say(f"Alice dropped by the server on quit: {leave(alice)}")
    say(f"Bob dropped by the server on quit: {leave(bob)}")
    srv.stop(papi)
    block("T0 vme.log: saves", log_lines("Saving PC", "Saving all players", "GUEST"))
    if os.path.exists(BACKUP):
        shutil.rmtree(BACKUP)
    shutil.copytree(PLY, BACKUP)
    evidence("Backup taken at T0 (healthy server, clean shutdown)", BACKUP)

    # T1: the incident (zone name wrong, Alice logs in and saves)
    set_zone_name(ZONE_GOOD, ZONE_BAD)
    srv = Server("t1").start()
    block("T1 vme.log: the renamed zone", zone_lines("t1", ZONE_GOOD, ZONE_BAD))
    papi = login("Papi", "t1")
    alice = login("Alice", "t1")
    t1_inv = alice.cmd("inventory")
    block("T1 Alice inventory after logging in (zone name wrong)", t1_inv)
    RESULTS["t1_alice"] = inv_flags(t1_inv)
    block("T1 Alice: look slime", alice.cmd("look slime"))
    block("T1 Alice: open slime", alice.cmd("open slime"))
    look_in = alice.cmd("look in slime")
    block("T1 Alice: look in slime", look_in)
    RESULTS["t1_slime_holds_bun"] = "honey bun" in look_in.lower()
    saved = leave(alice)
    block("T1 vme.log: Alice's login, the sliming and her save", log_lines("Alice[", "Sliming", "Saving PC Alice"))
    say(f"Alice connection dropped by the server: {saved}")

    # T2: Bob earns something that exists only on the live server
    bob = login("Bob", "t2")
    block("T2 Papi: where is everyone (look)", papi.cmd("look"))
    block("T2 Papi: load spatula", papi.cmd(f"load {SPATULA}"))
    block("T2 Papi: give spatula bob", papi.cmd("give spatula bob"))
    t2_inv = bob.cmd("inventory")
    block("T2 Bob inventory (new item)", t2_inv)
    if "spatula" not in t2_inv.lower():
        block("T2 Papi: goto bob (fallback)", papi.cmd("goto bob"))
        block("T2 Papi: give spatula bob (again)", papi.cmd("give spatula bob"))
        t2_inv = bob.cmd("inventory")
        block("T2 Bob inventory (second try)", t2_inv)
    need(t2_inv, "spatula", "Bob did not receive the spatula")
    RESULTS["t2_bob"] = inv_flags(t2_inv)
    leave(bob)
    srv.stop(papi)
    evidence("Live state at T2 versus the T0 backup (server stopped cleanly)", BACKUP)
    snapshot()
    starting_files("Snapshot of vme/lib and etc/zonelist taken after the T2 shutdown")
    summary("\nEach case below starts from a fresh copy of that snapshot.")


def alice_view(phase, label):
    """Log Alice in, record what she holds, try the box, quit."""
    c = login("Alice", phase)
    inv = c.cmd("inventory")
    block(f"{label}: Alice inventory", inv)
    return c, inv


def case1():
    summary("\n## Case 1: put the zone name back, restart, restore no player file")
    restore_snapshot()
    starting_files("Case 1 start: files copied back from the snapshot")
    set_zone_name(ZONE_BAD, ZONE_GOOD)
    srv = Server("c1a").start()
    block("Case 1 vme.log (boot with the zone name back)", zone_lines("c1a", ZONE_GOOD, ZONE_BAD))
    papi = login("Papi", "c1a")
    alice, inv = alice_view("c1a", "Case 1, first boot")
    RESULTS["c1a_alice"] = inv_flags(inv)
    block("Case 1: Alice look slime", alice.cmd("look slime"))
    block("Case 1: Alice look gift", alice.cmd("look gift"))
    block("Case 1: Alice open slime", alice.cmd("open slime"))
    look_in = alice.cmd("look in slime")
    block("Case 1: Alice look in slime", look_in)
    RESULTS["c1a_slime_holds_bun"] = "honey bun" in look_in.lower()
    leave(alice)
    bob = login("Bob", "c1a")
    binv = bob.cmd("inventory")
    block("Case 1, first boot: Bob inventory", binv)
    RESULTS["c1a_bob"] = inv_flags(binv)
    leave(bob)
    block("Case 1 vme.log: Sliming lines, first boot", log_lines("Sliming") or "(none)")
    srv.stop(papi)

    srv = Server("c1b").start()
    papi = login("Papi", "c1b")
    alice, inv = alice_view("c1b", "Case 1, after a clean restart")
    RESULTS["c1b_alice"] = inv_flags(inv)
    look_in = alice.cmd("look in slime")
    block("Case 1, after a clean restart: Alice look in slime", look_in)
    RESULTS["c1b_slime_holds_bun"] = "honey bun" in look_in.lower()
    leave(alice)
    bob = login("Bob", "c1b")
    binv = bob.cmd("inventory")
    block("Case 1, after a clean restart: Bob inventory", binv)
    RESULTS["c1b_bob"] = inv_flags(binv)
    leave(bob)
    block("Case 1 vme.log: Sliming lines, second boot", log_lines("Sliming") or "(none)")
    srv.stop(papi)
    evidence("Case 1 final files versus the T0 backup", BACKUP)


def case2():
    summary("\n## Case 2: put the zone name back and restore only Alice's inventory file")
    restore_snapshot()
    starting_files("Case 2 start: files copied back from the snapshot")
    set_zone_name(ZONE_BAD, ZONE_GOOD)
    src = os.path.join(BACKUP, "a", "alice.inv")
    shutil.copy2(src, ply("alice", ".inv"))
    say("copied backup alice.inv over the live one (server stopped, Alice offline)")
    block("Case 2: files after the copy", f"alice.inv live  {digest(ply('alice', '.inv'))}\n"
          f"alice.inv bkup  {digest(src)}\nbob.inv live    {digest(ply('bob', '.inv'))}")

    srv = Server("c2a").start()
    block("Case 2 vme.log (boot with the zone name back)", zone_lines("c2a", ZONE_GOOD, ZONE_BAD))
    papi = login("Papi", "c2a")
    alice, inv = alice_view("c2a", "Case 2, first boot")
    RESULTS["c2a_alice"] = inv_flags(inv)
    RESULTS["c2a_alice_after"], RESULTS["c2a_look_in_has_bun"] = box_check(alice, "Case 2, first boot")
    # put the bun back and close the box, so the check can be repeated after the restart
    block("Case 2, first boot: Alice put bun in gift", alice.cmd("put bun in gift"))
    again = look_in_box(alice)
    block("Case 2, first boot: Alice look in gift (bun put back)", again)
    RESULTS["c2a_bun_back_in_box"] = "honey bun" in again.lower()
    block("Case 2, first boot: Alice close gift", alice.cmd("close gift"))
    leave(alice)
    bob = login("Bob", "c2a")
    binv = bob.cmd("inventory")
    block("Case 2, first boot: Bob inventory", binv)
    RESULTS["c2a_bob"] = inv_flags(binv)
    leave(bob)
    block("Case 2 vme.log: Sliming lines, first boot", log_lines("Sliming") or "(none)")
    srv.stop(papi)

    srv = Server("c2b").start()
    papi = login("Papi", "c2b")
    alice, inv = alice_view("c2b", "Case 2, after a clean restart")
    RESULTS["c2b_alice"] = inv_flags(inv)
    RESULTS["c2b_alice_after"], RESULTS["c2b_look_in_has_bun"] = box_check(alice, "Case 2, after a clean restart")
    leave(alice)
    bob = login("Bob", "c2b")
    binv = bob.cmd("inventory")
    block("Case 2, after a clean restart: Bob inventory", binv)
    RESULTS["c2b_bob"] = inv_flags(binv)
    leave(bob)
    block("Case 2 vme.log: Sliming lines, second boot", log_lines("Sliming") or "(none)")
    srv.stop(papi)
    evidence("Case 2 final files versus the T0 backup", BACKUP)


def verdict():
    r = RESULTS
    rows = [
        ("T0, before the backup: Alice's pack", show(r.get("t0_alice"))),
        ("T1, zone name wrong, Alice logs in: her pack", show(r.get("t1_alice"))),
        ("T1: honey bun listed inside the slime (`look in slime`)", r.get("t1_slime_holds_bun")),
        ("T2: Bob's pack", show(r.get("t2_bob"))),
        ("Case 1, first boot: Alice's pack", show(r.get("c1a_alice"))),
        ("Case 1, first boot: honey bun listed inside the slime", r.get("c1a_slime_holds_bun")),
        ("Case 1, after a clean restart: Alice's pack", show(r.get("c1b_alice"))),
        ("Case 1, after a clean restart: honey bun listed inside the slime", r.get("c1b_slime_holds_bun")),
        ("Case 1, after a clean restart: Bob's pack", show(r.get("c1b_bob"))),
        ("Case 2, first boot: Alice's pack", show(r.get("c2a_alice"))),
        ("Case 2, first boot: honey bun listed in the box before taking it out", r.get("c2a_look_in_has_bun")),
        ("Case 2, first boot: Alice's pack after taking it out", show(r.get("c2a_alice_after"))),
        ("Case 2, first boot: bun back in the box before she quit", r.get("c2a_bun_back_in_box")),
        ("Case 2, first boot: Bob's pack", show(r.get("c2a_bob"))),
        ("Case 2, after a clean restart: Alice's pack", show(r.get("c2b_alice"))),
        ("Case 2, after a clean restart: honey bun listed in the box before taking it out", r.get("c2b_look_in_has_bun")),
        ("Case 2, after a clean restart: Alice's pack after taking it out", show(r.get("c2b_alice_after"))),
        ("Case 2, after a clean restart: Bob's pack", show(r.get("c2b_bob"))),
    ]
    lines = ["| Check | Read from the game text |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows]
    summary("\n## What the game showed (read from the text by the harness; the transcripts hold the real output)\n\n"
            + "\n".join(lines))


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    try:
        if what in ("all", "setup"):
            setup()
        if what in ("all", "case1"):
            case1()
        if what in ("all", "case2"):
            case2()
        if what == "selftest":
            selftest()
        verdict()
    except BaseException:
        tb = traceback.format_exc()
        block("HARNESS FAILURE", tb)
        for c in OPEN:
            try:
                block(f"last text seen by {c.name} ({c.phase})", c.tail(2500))
            except Exception:
                pass
        block("vme.log tail", read_log()[-4000:])
        kill_leftovers()
        sys.exit(1)
    finally:
        if not MOCK:
            kill_leftovers()


def selftest():
    """Exercise the primitives against the mock server (DM3_MOCK=1)."""
    papi = login("Alice", "selftest", new=True)
    out = papi.cmd("inventory")
    assert "bread" in out.lower(), out
    assert leave(papi)
    again = login("Alice", "selftest")
    assert "bread" in again.cmd("inventory").lower()
    leave(again)
    say("selftest ok")


if __name__ == "__main__":
    main()
