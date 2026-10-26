"""Simulated NVIDIA card and game, to preview and try the panel on a PC without one.

Nothing here touches a GPU. Watts and temperatures are invented but react like
the real board: our load adds power by intensity and time, a running game adds
its own, both share GPU time, and readings lag like NVML's ~1 s average.
"""
import collections
import math
import random
import threading
import time
from sensors import check_temperatures

IDLE, FULL, LIMIT = 35.0, 340.0, 360.0
SMS = 84               # RTX 5080
UNIT = .006            # seconds of GPU work per simulated unit
WINDOW = .5            # how far back our activity is averaged
LOADING = 6.0          # seconds of loading screen before the game renders
LOADING_DEMAND = .12   # share of the card a loading screen uses


class SimulatedGpu:
    """Stands in for Telemetry, Compute, the sensors and the process list."""
    devices = [(None, 'RTX 5080 SIMULADA (no carga ninguna tarjeta)', 'GPU-simulada')]

    def __init__(self, seed=1):
        self.lock = threading.Lock()
        self.units = collections.deque()  # (finished at, seconds, intensity)
        self.game = None                  # (exe, opened at, watts)
        self.watts, self.core = IDLE, 34.0
        self.last = time.monotonic()
        self.random = random.Random(seed)

    # The simulated game.
    def open_game(self, exe, watts):
        self.game = (exe, time.monotonic(), float(watts))

    def close_game(self):
        self.game = None

    def processes(self):
        game = self.game
        return {game[0]} if game else set()

    def game_demand(self, now):
        game = self.game
        if game is None:
            return 0.0
        exe, opened, watts = game
        if now-opened < LOADING:
            return LOADING_DEMAND
        demand = (watts-IDLE)/(FULL-IDLE) * (1 + .04*math.sin(now*1.7))
        return max(0.0, min(1.0, demand))

    # Our simulated load.
    def compute(self, uuid, mode='Core', vram_mb=512):
        return SimulatedCompute(self)

    def record(self, seconds, intensity):
        active = max(1, min(SMS, round(intensity*SMS)))
        with self.lock:
            self.units.append((time.monotonic(), seconds, active/SMS))

    def activity(self, now):
        """(busy share of time, average intensity) over the last WINDOW seconds."""
        with self.lock:
            while self.units and self.units[0][0] < now-WINDOW:
                self.units.popleft()
            busy = sum(seconds for _, seconds, _ in self.units)
            work = sum(seconds*intensity for _, seconds, intensity in self.units)
        return min(1.0, busy/WINDOW), min(1.0, work/WINDOW)

    # Telemetry interface.
    def read(self, _handle):
        now = time.monotonic()
        dt, self.last = max(0.0, now-self.last), now
        busy, work = self.activity(now)
        game = self.game_demand(now)
        # Shared GPU time: each one loses part of its turn while the other works.
        target = IDLE + (FULL-IDLE) * (work*(1-game/2) + game*(1-busy/2))
        self.watts += (target-self.watts) * (1-math.exp(-dt/.6))
        self.core += (30+self.watts*.115-self.core) * (1-math.exp(-dt/6))
        return round(self.watts + self.random.uniform(-1.5, 1.5), 1), int(self.core), LIMIT

    def sensors(self, monitor_vram=True):
        return SimulatedSensors(self, monitor_vram)

    def shutdown(self):
        pass


class SimulatedCompute:
    def __init__(self, gpu):
        self.gpu = gpu

    def batch(self, intensity):
        time.sleep(UNIT)
        self.gpu.record(UNIT, intensity)

    def drain(self):
        return UNIT

    def close(self):
        pass


class SimulatedSensors:
    def __init__(self, gpu, monitor_vram=True):
        self.gpu, self.monitor_vram = gpu, monitor_vram

    def read(self):
        core = round(self.gpu.core, 1)
        memory = round(core+7.5, 1) if self.monitor_vram else None
        return check_temperatures((core, memory), require_memory=self.monitor_vram)
