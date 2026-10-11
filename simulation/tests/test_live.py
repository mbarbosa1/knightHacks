import pathlib, sys
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sim.live import LiveRun, TAIL_S


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


class Store:
    def __init__(self): self.saved, self.fail = [], False
    def save_observation(self, obs):
        if self.fail:
            raise ConnectionError('backend unavailable')
        self.saved.append(obs['id'])


def reports(*times):
    return [{'observation': {'id': f'r{i}', 'sim_time_s': t}} for i, t in enumerate(times)]


def live(*times, speed=20):
    clock, store, ended = Clock(), Store(), []
    run = LiveRun('00000000-run', reports(*times), store, speed=speed, clock=clock, on_finish=ended.append)
    with run._lock:
        run._playing, run._last = True, clock()   # started, without the background thread
    return run, clock, store, ended


def test_reports_are_released_when_their_time_comes_and_in_order():
    run, clock, store, _ = live(300, 30, 100)          # given out of order
    run.tick()
    assert store.saved == []
    clock.t += 2                                        # 2 real seconds at 20x = 40 simulated
    run.tick()
    assert store.saved == ['r1'] and run.state()['sim_time_s'] == 40
    clock.t += 4                                        # 120 simulated
    run.tick()
    assert store.saved == ['r1', 'r2']
    assert run.state()['reports_released'] == 2 and run.state()['reports_total'] == 3


def test_speed_change_and_pause_do_not_make_the_clock_jump():
    run, clock, store, _ = live(1000)
    clock.t += 5
    assert run.set(speed=10)['sim_time_s'] == 100       # 5 s at 20x, then the new speed applies from here
    clock.t += 5
    assert run.state()['sim_time_s'] == 150
    run.set(playing=False)
    clock.t += 60
    assert run.state()['sim_time_s'] == 150             # paused
    run.set(playing=True)
    clock.t += 1
    assert run.state()['sim_time_s'] == 160


def test_a_failed_save_is_retried_and_order_is_kept():
    run, clock, store, _ = live(10, 20, 30)
    clock.t += 5
    store.fail = True
    run.tick()
    assert store.saved == [] and 'unavailable' in run.state()['last_error']
    store.fail = False
    run.tick()
    assert store.saved == ['r0', 'r1', 'r2'] and run.state()['last_error'] is None


def test_the_run_finishes_once_everything_is_out_and_the_tail_has_passed():
    run, clock, store, ended = live(40)
    clock.t += 3
    run.tick()
    assert store.saved == ['r0'] and not run.finished
    clock.t += 10                                       # far past 40 + TAIL_S simulated
    run.tick()
    assert run.finished and ended == [run] and run.state()['sim_time_s'] == 40 + TAIL_S
    run.set(playing=True)
    assert run.state()['playing'] is False              # a finished run cannot be restarted


def test_stop_ends_it_early_and_only_once():
    run, clock, store, ended = live(500)
    run.stop(); run.stop()
    assert run.finished and ended == [run] and store.saved == []


def test_the_background_thread_releases_reports():
    import time
    store, ended = Store(), []
    run = LiveRun('00000000-run', reports(1, 2), store, speed=20, on_finish=ended.append).start()
    deadline = time.monotonic() + 3
    while len(store.saved) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    run.stop()
    assert store.saved == ['r0', 'r1'] and ended == [run]
