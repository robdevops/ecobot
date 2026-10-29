"""Where the log lines go (stdout or stderr), their priorities for systemd's journal, and the hints for a name Docker can't find."""
import io
import logging
import os

import httpx

from lib import netenv
from lib.logs import LogFormat, on_journal, stream_handlers


def logger_with(fmt, out, err):
    log = logging.getLogger("tests.logs")
    log.handlers[:] = stream_handlers(fmt, out, err)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log


def test_debug_and_info_go_to_stdout_and_warnings_and_errors_to_stderr():
    out, err = io.StringIO(), io.StringIO()
    log = logger_with(LogFormat("%(levelname)s %(message)s"), out, err)
    log.debug("detail")
    log.info("ordinary")
    log.warning("careful")
    log.error("broken")
    assert out.getvalue() == "DEBUG detail\nordinary\n" and err.getvalue() == "WARN careful\nERROR broken\n"


def test_on_the_journal_every_line_starts_with_its_priority_a_tracebacks_too():
    out, err = io.StringIO(), io.StringIO()
    log = logger_with(LogFormat("%(levelname)s %(message)s", priority=True), out, err)
    log.info("ordinary")
    log.warning("careful")
    try:
        raise KeyError("x")
    except KeyError:
        log.exception("broken")
    assert out.getvalue() == "<6>ordinary\n"
    lines = err.getvalue().splitlines()
    assert lines[0] == "<4>WARN careful" and lines[1] == "<3>ERROR broken" and all(line.startswith("<3>") for line in lines[1:])


def test_the_journal_is_the_stream_systemd_names_and_nothing_else(tmp_path, monkeypatch):
    with open(tmp_path / "f", "w") as f:
        found = os.fstat(f.fileno())
        monkeypatch.setenv("JOURNAL_STREAM", f"{found.st_dev}:{found.st_ino}")
        assert on_journal(f)
        monkeypatch.setenv("JOURNAL_STREAM", "1:2")
        assert not on_journal(f)
        monkeypatch.delenv("JOURNAL_STREAM")
        assert not on_journal(f) and not on_journal(io.StringIO())                 # no systemd, or a stream with no file


def test_dockers_own_network_is_told_from_the_hosts_by_the_bridges_the_host_has(monkeypatch):
    monkeypatch.setattr(netenv.os.path, "exists", lambda path: path == "/.dockerenv")
    monkeypatch.setattr(netenv.os, "listdir", lambda path: ["eth0", "lo"])
    assert netenv.docker_private_network()
    monkeypatch.setattr(netenv.os, "listdir", lambda path: ["br-3f2a", "docker0", "enp3s0", "lo", "veth12"])
    assert not netenv.docker_private_network()                                       # network_mode: host
    monkeypatch.setattr(netenv.os.path, "exists", lambda path: False)
    assert not netenv.docker_private_network()                                       # not in Docker at all


async def test_an_airgradient_that_does_not_answer_says_so_and_in_dockers_network_how_to_fix_it(monkeypatch, caplog):
    from lib.airgradient.local import LocalSensor

    async def nowhere(host):
        return None
    caplog.set_level(logging.WARNING)
    for private, cloud in ((True, True), (False, False)):
        caplog.clear()
        monkeypatch.setattr(netenv, "docker_private_network", lambda: private)
        sensor = LocalSensor("http://airgradient_d83bda1c1e08.local", transport=httpx.MockTransport(lambda r: httpx.Response(500)),
                             resolve=nowhere, cloud=cloud)
        assert await sensor.read() is None
        text = caplog.text
        assert ("AirGradient: (airgradient_d83bda1c1e08.local) did not answer (OSError: airgradient_d83bda1c1e08.local was not found on "
                "the network); " + ("using the cloud, " if cloud else "") + "trying again in 5 min") in text
        assert ("put the IP address in AIRGRADIENT_LOCAL_URL, or put the container on the host network" in text) == private
        await sensor.close()
