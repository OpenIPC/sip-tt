"""The out-of-band lever, and the ways it can fail.

Everything here matters for one reason: the caller catches `TriggerError` and
reports "we could not ask the device to place a call". Anything that escapes
as a raw exception crashes the run instead, and a crash mid-conformance reads
as though the device did something. It did not — we did.
"""

import re

import pytest

from sip_tt.runtime import trigger as T
from sip_tt.runtime.profile import Trigger


def test_no_trigger_is_a_named_failure():
    with pytest.raises(T.TriggerError, match="no trigger"):
        T.fire(Trigger(kind="none"))


def test_a_successful_command_reports_what_it_ran():
    assert "/bin/true" in T.fire(Trigger(kind="command", command="/bin/true"))


def test_a_failing_command_becomes_a_trigger_error():
    with pytest.raises(T.TriggerError, match="exited 1"):
        T.fire(Trigger(kind="command", command="/bin/false"))


def test_a_missing_executable_becomes_a_trigger_error():
    with pytest.raises(T.TriggerError, match="not found on PATH"):
        T.fire(Trigger(kind="command", command="/nonexistent/please-no"))


def test_an_unparseable_command_becomes_a_trigger_error():
    """shlex raises ValueError on an unbalanced quote."""
    with pytest.raises(T.TriggerError, match="does not parse"):
        T.fire(Trigger(kind="command", command="echo 'unbalanced"))


def test_an_empty_command_becomes_a_trigger_error():
    with pytest.raises(T.TriggerError, match="no command"):
        T.fire(Trigger(kind="command", command=""))


def test_a_hanging_command_becomes_a_trigger_error():
    with pytest.raises(T.TriggerError, match="did not finish"):
        T.fire(Trigger(kind="command", command="/bin/sleep 5"), timeout=0.3)


def test_a_hangup_needs_its_own_command():
    with pytest.raises(T.TriggerError, match="hangup_command"):
        T.fire(Trigger(kind="command", command="/bin/true"), hangup=True)


def test_the_digest_target_keeps_the_query():
    """RFC 7616 §3.4.6 — the digest covers path *and* query.

    majestic's own trigger is /api/v1/sip/call?target=sip:1001@host, so
    dropping the query means the retry can never authenticate.
    """
    trig = Trigger(kind="http", username="root", password="pw", method="POST")
    header = T._authorise('Digest realm="r", nonce="n", qop="auth"',
                          "http://cam/api/v1/sip/call?target=sip:1001@host",
                          trig)
    uri = re.search(r'uri="([^"]*)"', header).group(1)
    assert uri == "/api/v1/sip/call?target=sip:1001@host"


def test_a_url_without_a_query_is_unchanged():
    trig = Trigger(kind="http", username="u", password="p", method="POST")
    header = T._authorise('Digest realm="r", nonce="n"', "http://cam/call", trig)
    assert re.search(r'uri="([^"]*)"', header).group(1) == "/call"


def test_basic_auth_is_answered_when_that_is_what_was_asked_for():
    trig = Trigger(kind="http", username="u", password="p", method="POST")
    assert T._authorise('Basic realm="r"', "http://cam/x", trig).startswith("Basic ")


def test_an_unknown_scheme_is_not_guessed_at():
    trig = Trigger(kind="http", username="u", password="p", method="POST")
    assert T._authorise('Negotiate', "http://cam/x", trig) is None
