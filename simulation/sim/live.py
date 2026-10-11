"""The live loop: one run playing out on a clock.

A LiveRun owns the simulation clock for one run and makes things happen when their time comes.
For now that is releasing reports; unit movement and mid-run events hook into the same tick.

    simulated seconds = real seconds elapsed x speed        (speed is 10 or 20 in the UI)

Only one run is live at a time (see sim/server.py): starting another stops this one.
Nothing here knows whether the storage is local files or the backend API.
"""
import threading, time

SPEEDS = (10, 20)         # 30x was dropped: a big earthquake's reports could not all be saved in time
DEFAULT_SPEED = 20
TICK_S = 0.25            # real seconds between ticks
TAIL_S = 60              # simulated seconds the clock keeps running after the last scheduled thing


class LiveRun:
    def __init__(self, run_id, reports, storage, speed=DEFAULT_SPEED, clock=time.monotonic, on_finish=None):
        """reports: [{'observation': {...}, ...}] in any order; each is released at its sim_time_s.
        storage: anything with save_observation(obs). clock: injectable for tests."""
        self.run_id = run_id
        self._queue = sorted((item['observation'] for item in reports), key=lambda o: o['sim_time_s'])
        self.total = len(self._queue)
        self.end_s = (self._queue[-1]['sim_time_s'] if self._queue else 0) + TAIL_S
        self._storage, self._clock, self._on_finish = storage, clock, on_finish
        self._lock = threading.Lock()
        self._speed, self._playing = speed, False
        self._sim_s, self._last = 0.0, None     # simulated time banked so far, and when we last banked it
        self._released, self._last_error = 0, None
        self._stop, self._thread, self.finished = threading.Event(), None, False

    # --- the clock ---------------------------------------------------------------------------------
    def _advance(self):
        """Bank the simulated time since the last call. Caller holds the lock."""
        now = self._clock()
        if self._playing and self._last is not None:
            self._sim_s = min(self.end_s, self._sim_s + (now - self._last) * self._speed)
        self._last = now
        return self._sim_s

    def set(self, speed=None, playing=None):
        """Change speed or pause/resume without the clock jumping."""
        with self._lock:
            self._advance()
            if speed is not None:
                self._speed = speed
            if playing is not None and not self.finished:
                self._playing = playing
        return self.state()

    def state(self):
        with self._lock:
            return {'run_id': self.run_id, 'sim_time_s': round(self._advance(), 1), 'end_s': round(self.end_s, 1),
                    'speed': self._speed, 'playing': self._playing, 'finished': self.finished,
                    'reports_released': self._released, 'reports_total': self.total, 'last_error': self._last_error}

    # --- what happens as time passes ---------------------------------------------------------------
    def tick(self):
        """Release every report that is due. A report that fails to save stays first in line and is
        retried on the next tick, so reports always reach the agents in order."""
        with self._lock:
            now_s = self._advance()
            due = []
            while self._released + len(due) < self.total and self._queue[self._released + len(due)]['sim_time_s'] <= now_s:
                due.append(self._queue[self._released + len(due)])
        for obs in due:
            try:
                self._storage.save_observation(obs)
            except Exception as exc:   # network trouble: keep the clock running, try again next tick
                with self._lock:
                    self._last_error = str(exc)
                return
            with self._lock:
                self._released += 1
                self._last_error = None
        with self._lock:
            done = self._sim_s >= self.end_s and self._released == self.total
        if done:
            self._finish()

    def _finish(self):
        with self._lock:
            if self.finished:
                return
            self.finished, self._playing = True, False
        self._stop.set()
        if self._on_finish:
            self._on_finish(self)

    # --- running it --------------------------------------------------------------------------------
    def start(self):
        with self._lock:
            self._playing, self._last = True, self._clock()
        self._thread = threading.Thread(target=self._loop, name=f'live-{self.run_id[:8]}', daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(TICK_S):
            self.tick()

    def stop(self):
        """Stop the clock for good (a new run started, or the simulator is shutting down)."""
        self._finish()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
