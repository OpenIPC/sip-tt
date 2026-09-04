"""Making the device place a call.

An ONVIF device is a server: onvif-tt points at it and it answers. A SIP user
agent that *originates* answers to nobody — it calls when something tells it
to, and that something is outside SIP. A doorbell has a button. A camera has
an HTTP API, or a GPIO, or a motion event.

So the originating purposes need an out-of-band lever, the profile declares
which one this device has, and where there is none those purposes are reported
as not-run rather than passed. That distinction is the reason this module
exists at all; the mechanics below are the easy part.

Four kinds:

* ``none``     — the device cannot be asked. Originating purposes fail as
                 not-exercised, with a message saying to configure a trigger.
* ``http``     — a URL to POST (or GET). Digest and basic auth are both tried,
                 because embedded web servers disagree about which they want.
* ``command``  — a shell command. This is the general escape hatch: a GPIO
                 script, an ``adb shell input tap``, an expect script driving a
                 softphone's stdin.
* ``manual``   — print the instruction and wait for a human. Useful once, when
                 bringing up a new device; useless in CI, and it says so.
"""

from __future__ import annotations

import base64
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .auth import Challenge, respond


class TriggerError(RuntimeError):
    """The lever did not work. Never a verdict about the device's SIP."""


def fire(trigger, *, hangup: bool = False, timeout: float = 10.0) -> str:
    """Ask the device to place (or end) a call. Returns a short description."""
    if not trigger.available:
        raise TriggerError(
            "the profile declares no trigger, so the device cannot be asked "
            "to place a call")

    if trigger.kind == "http":
        url = trigger.hangup_url if hangup else trigger.url
        if not url:
            raise TriggerError(
                f"trigger kind 'http' with no {'hangup_url' if hangup else 'url'}")
        return _http(url, trigger, timeout)

    if trigger.kind == "command":
        cmd = trigger.hangup_command if hangup else trigger.command
        if not cmd:
            raise TriggerError(
                f"trigger kind 'command' with no "
                f"{'hangup_command' if hangup else 'command'}")
        return _command(cmd, timeout)

    if trigger.kind == "manual":
        what = "hang up" if hangup else "place a call to sip-tt"
        print(f"\n>>> Please {what} on the device now, then press Enter.",
              flush=True)
        try:
            input()
        except EOFError:
            raise TriggerError(
                "trigger kind 'manual' needs someone at a terminal, and "
                "stdin is closed — this run is not interactive") from None
        return f"operator confirmed: {what}"

    raise TriggerError(f"unknown trigger kind {trigger.kind!r}")


def _http(url: str, trigger, timeout: float) -> str:
    req = urllib.request.Request(url, method=trigger.method, data=b"")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return f"{trigger.method} {url} -> {r.status}"
    except urllib.error.HTTPError as e:
        if e.code != 401 or not trigger.username:
            raise TriggerError(
                f"{trigger.method} {url} -> {e.code} {e.reason}") from None
        header = e.headers.get("WWW-Authenticate", "")
        retry = _authorise(header, url, trigger)
        if retry is None:
            raise TriggerError(
                f"{url} asked for authentication we cannot provide: "
                f"{header[:60]!r}") from None
        req2 = urllib.request.Request(url, method=trigger.method, data=b"")
        req2.add_header("Authorization", retry)
        try:
            with urllib.request.urlopen(req2, timeout=timeout) as r:
                return f"{trigger.method} {url} -> {r.status} (authenticated)"
        except urllib.error.HTTPError as e2:
            raise TriggerError(
                f"{trigger.method} {url} -> {e2.code} {e2.reason} after "
                f"authenticating as {trigger.username!r}") from None
    except OSError as e:
        raise TriggerError(f"{url} unreachable: {e}") from None


def _authorise(header: str, url: str, trigger) -> str | None:
    """Answer a WWW-Authenticate, digest or basic.

    The digest is over the URL's path, not the whole URL — the same rule as
    SIP's, and the usual reason a hand-rolled retry gets a 401 twice.
    """
    scheme = header.split(" ", 1)[0].lower() if header else ""
    if scheme == "digest":
        ch = Challenge.parse(header)
        if ch is None:
            return None
        path = urllib.parse.urlsplit(url).path or "/"
        return respond(ch, username=trigger.username,
                       password=trigger.password, method=trigger.method,
                       uri=path)
    if scheme == "basic":
        raw = f"{trigger.username}:{trigger.password}".encode()
        return "Basic " + base64.b64encode(raw).decode()
    return None


def _command(cmd: str, timeout: float) -> str:
    proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise TriggerError(
            f"{cmd!r} exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:200]}")
    return f"ran {cmd!r}"
