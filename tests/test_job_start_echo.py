"""A P1S's first report of a new print still shows the last one done (100 %, nothing left): no
1-second «print» before the real one (14 of them on the farm, 19.10-17.11)."""
from app.services.job_journal import JobMachine, Sample
from tests.test_job_journal import NAME, PID, T0, run, seg, seq


def _echo(t, seq_no=None, started=None, print_time=0, layer=137):
    return Sample(ts=t, state="printing", progress=100, eta=0, print_time=print_time, layer=layer, total_layers=137,
                  name=NAME, kind="bambu", seq=seq_no, seq_started_at=started)


def _after_finish(seq_no=5):
    """The last print done: FINISH 100 % for a while, the poll every 30 s."""
    return seq(seg(4, "finished", progress=100, eta=0, name=NAME, seq=seq_no), step=30)


def _real(t0, seq_no=None, n=40):
    return seq(seg(n, "printing", name=NAME, progress=(0, 99), eta=(6000, 60), layer=(0, 137), total_layers=137,
                   seq=seq_no),
               seg(1, "finished", name=NAME, progress=100, eta=0, seq=seq_no), t0=t0, step=150)


def test_live_start_echo_is_one_print_from_the_start_seen():
    ss = _after_finish()
    t = ss[-1].ts + 20
    ss += [_echo(t, seq_no=6, started=t - 1)] + _real(t + 31, seq_no=6)
    r = run(ss)
    assert len(r.rows) == 1
    row = r.rows[0]
    assert row["started_at"] == t - 1 and row["start_exact"] == 1          # when the printer started it
    assert row["result"] == "finished" and row["end_progress"] == 100
    assert row["planned_sec"] == 31 + 1 + 6000                              # preparation counted in


def test_history_start_echo_without_job_seq():
    # the backfill (and printers without job_seq): the start is the echo's sample, after the idle
    ss = _after_finish(seq_no=None)
    t = ss[-1].ts + 30
    ss += [_echo(t, print_time=18)] + _real(t + 32)
    r = run(ss)
    assert len(r.rows) == 1 and r.rows[0]["started_at"] == t and r.rows[0]["start_exact"] == 1


def test_echo_lasting_several_polls_still_one_print():
    ss = _after_finish()
    t = ss[-1].ts + 20
    ss += [_echo(t, seq_no=6, started=t - 1), _echo(t + 30, seq_no=6, started=t - 1), _echo(t + 60, seq_no=6, started=t - 1)]
    ss += _real(t + 90, seq_no=6)
    r = run(ss)
    assert len(r.rows) == 1 and r.rows[0]["started_at"] == t - 1 and r.rows[0]["result"] == "finished"


def test_echo_then_back_to_finish_records_nothing():
    # started and stopped during the preparation: nothing was printed
    ss = _after_finish()
    t = ss[-1].ts + 20
    ss += [_echo(t, seq_no=6, started=t - 1)]
    ss += seq(seg(3, "finished", progress=100, eta=0, name=NAME, seq=6), t0=t + 30, step=30)
    ss += _real(ss[-1].ts + 600, seq_no=7)
    ss[-41] = Sample(**dict(vars(ss[-41]), seq_started_at=ss[-41].ts - 5))
    r = run(ss)
    assert len(r.rows) == 1 and r.rows[0]["started_at"] == ss[-41].ts - 5


def test_a_print_first_seen_at_its_end_is_still_recorded():
    # no start seen (the panel came up during the last seconds): finished, as before
    m = JobMachine(PID, kind="bambu")
    r = run([_echo(T0), Sample(ts=T0 + 30, state="finished", progress=100, eta=0, name=NAME, kind="bambu")], m=m)
    assert len(r.rows) == 1 and r.rows[0]["result"] == "finished" and r.rows[0]["start_exact"] == 0
