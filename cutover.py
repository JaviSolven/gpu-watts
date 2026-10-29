"""Process readiness conditions and Windows process CPU counters."""
import ctypes as C
import math
import os

MODES = ('Al aparecer', 'GPU supera W', 'CPU del proceso supera %', 'GPU o CPU', 'GPU y CPU')


def read_cpu_times(pids):
    lib = C.WinDLL('kernel32', use_last_error=True)
    op = lib.OpenProcess
    op.argtypes, op.restype = [C.c_uint, C.c_int, C.c_uint], C.c_void_p
    gt = lib.GetProcessTimes
    gt.argtypes = [C.c_void_p] + [C.POINTER(C.c_uint64)]*4
    gt.restype = C.c_int
    close = lib.CloseHandle
    close.argtypes, close.restype = [C.c_void_p], C.c_int
    result = {}
    for pid in pids:
        handle = op(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = C.get_last_error()
            if error in (87, 1168):  # exited during enumeration
                continue
            raise RuntimeError(f'No se puede leer CPU de PID {pid}: Windows {error}')
        try:
            created, exited, kernel, user = (C.c_uint64() for _ in range(4))
            if not gt(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
                raise RuntimeError(f'GetProcessTimes PID {pid}: Windows {C.get_last_error()}')
            # Creation time guards against PID reuse.
            result[(pid, created.value)] = (kernel.value+user.value)/1e7
        finally:
            close(handle)
    return result


class Cutover:
    def __init__(self, mode='GPU supera W', watts=160, cpu=10, hold=2, grace=2, processors=None):
        if mode not in MODES:
            raise ValueError('Condición de relevo desconocida.')
        if not all(math.isfinite(v) for v in (watts,cpu,hold,grace)):
            raise ValueError('Los umbrales deben ser números finitos.')
        if not 10 <= watts <= 1000 or not 0 < cpu <= 100 or not 0 <= hold <= 60 or not 0 <= grace <= 120:
            raise ValueError('Rangos de relevo: 10–1000 W; CPU >0–100%; mantener 0–60 s; espera 0–120 s.')
        self.mode, self.watts, self.cpu = mode, watts, cpu
        self.hold, self.grace = hold, grace
        self.processors = processors or os.cpu_count() or 1
        self.first_seen = self.since = self.previous_time = None
        self.previous = {}
        self.cpu_percent = None
        self.present = False
        self.description = 'Esperando al proceso'

    @property
    def uses_gpu(self):
        return self.mode in ('GPU supera W','GPU o CPU','GPU y CPU')

    @property
    def uses_cpu(self):
        return self.mode in ('CPU del proceso supera %','GPU o CPU','GPU y CPU')

    def update(self, now, present, watts, counters=None):
        self.present = present
        self.cpu_percent = None
        if not present:
            self.first_seen = self.since = self.previous_time = None
            self.previous = {}
            self.description = 'Esperando al proceso'
            return False
        if self.first_seen is None:
            self.first_seen = now
        if self.mode == 'Al aparecer':
            self.description = 'Proceso detectado'
            return True
        if self.uses_cpu:
            counters = counters or {}
            if self.previous_time is not None and now > self.previous_time:
                delta = sum(max(0, value-self.previous[key]) for key,value in counters.items() if key in self.previous)
                self.cpu_percent = min(100, delta/(now-self.previous_time)/self.processors*100)
            self.previous, self.previous_time = counters, now
        gpu_ok = watts >= self.watts
        cpu_ok = self.cpu_percent is not None and self.cpu_percent >= self.cpu
        ok = {'GPU supera W':gpu_ok, 'CPU del proceso supera %':cpu_ok,
              'GPU o CPU':gpu_ok or cpu_ok, 'GPU y CPU':gpu_ok and cpu_ok}[self.mode]
        cpu_text = '—' if self.cpu_percent is None else f'{self.cpu_percent:.1f}%'
        self.description = f'Proceso abierto · GPU total {watts:.0f} W · CPU proceso {cpu_text}'
        if now-self.first_seen < self.grace:
            self.since = None
            self.description += ' · espera inicial'
            return False
        if not ok:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        elapsed = now-self.since
        self.description += f' · condición {elapsed:.1f}/{self.hold:.1f} s'
        return elapsed >= self.hold


ADAPTIVE_MODE = 'Relevo adaptativo (mantener base)'
MODES = (ADAPTIVE_MODE,) + MODES


class AdaptiveHandoff:
    """Control our duty from board power. Never claims to measure game-only watts.

    The board must hold 95 % of the base without us. A game that draws less
    than that would keep us supporting it until the time limit, so once the
    game's own part (estimated by the caller) reaches TAKEOVER of the base
    for a second it is really playing, not loading: from then on we only
    withdraw, and the handoff ends when our load reaches zero.

    With `watch` seconds, after taking over we stay watching that long from the
    game's appearance: a deep fall of the game's part (a loading screen, under
    REARM of the base) brings support back, so the next jump (into the level)
    starts from the base again. The handoff cannot end before the watch does."""
    TAKEOVER, TAKEOVER_SECONDS, REARM = .7, 1.0, .5

    def __init__(self, target, ramp_seconds=5, hold=2, grace=2, watch=0):
        self.target = target
        self.ramp_seconds = max(.5, ramp_seconds)
        self.hold, self.grace, self.watch = hold, grace, watch
        self.first_seen = self.previous_time = self.since = None
        self.ceiling = None
        self.game_since, self.committed = None, False
        self.played = False  # the game took over at least once (kept if it closes)

    def watching(self, now):
        return self.watch > 0 and self.first_seen is not None and now-self.first_seen < self.watch

    def update(self, now, present, measured, duty, power_limit, game_watts=None):
        if not present:
            self.first_seen = self.previous_time = self.since = None
            self.ceiling = None
            self.game_since, self.committed = None, False
            return duty, False, 'Preparando/manteniendo la base antes del proceso'
        if self.first_seen is None:
            self.first_seen = now
            self.previous_time = now
            self.ceiling = max(0, duty)
        dt = max(0, min(1, now-self.previous_time))
        self.previous_time = now
        if game_watts is not None and game_watts >= self.TAKEOVER*self.target:
            if self.game_since is None:
                self.game_since = now
            self.committed = self.committed or now-self.game_since >= self.TAKEOVER_SECONDS
        else:
            self.game_since = None
        if self.committed and self.watching(now) and game_watts is not None and game_watts < self.REARM*self.target:
            self.committed, self.game_since = False, None
        self.played = self.played or self.committed
        max_step = self.ceiling*dt/self.ramp_seconds
        low = self.target*.95
        error = self.target-measured
        if self.committed:
            # Withdraw at the chosen smoothness and never support again.
            duty = max(0, duty-max_step)
        elif measured >= low:
            # Probe gradual withdrawal even inside the power deadband. If the
            # game cannot sustain the base, the next low readings restore support.
            wanted = max(.003, (measured-low)/max(power_limit,1)*.6)
            duty = max(0, duty-min(max_step,wanted))
        elif measured < low:
            wanted = max(0,error/max(power_limit,1)*.6)
            duty = min(self.ceiling,duty+min(max_step,wanted))
        if duty < .000001:
            duty = 0.0
        ready = duty == 0 and (measured >= low or self.committed) and now-self.first_seen >= max(self.grace, self.watch)
        if ready:
            if self.since is None:
                self.since = now
        else:
            self.since = None
        confirmed = self.since is not None and now-self.since >= self.hold
        text = f'Relevo adaptativo · GPU total {measured:.0f}/{self.target:.0f} W · aportación programada {duty*100:.1f}%'
        if self.watching(now):
            text += f' · vigilando {self.watch-(now-self.first_seen):.0f} s más'
        elif self.committed:
            text += ' · el juego ya juega: retirada definitiva'
        if self.since is not None:
            text += f' · sin carga propia {now-self.since:.1f}/{self.hold:g} s'
        return duty, confirmed, text
