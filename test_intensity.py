import ctypes as C
import math
import queue
import threading
import unittest
from unittest.mock import patch
import gpu_watts as app
from cutover import Cutover, ADAPTIVE_MODE
from test_control import FakeCompute, FakeSensors, FakeTelemetry


class RecordingCompute:
    def __init__(self):
        self.calls = []
    def batch(self, intensity):
        self.calls.append(('batch', intensity))
    def drain(self):
        self.calls.append(('drain',))
        return .006


class RecordingStop:
    def __init__(self):
        self.waits = []
    def wait(self, seconds):
        self.waits.append(seconds)


class IntensityTests(unittest.TestCase):
    def test_alone_runs_continuously_at_duty_intensity(self):
        self.assertEqual(app.load_shape(.3), (.3, 1.0))
        self.assertEqual(app.load_shape(.3, None), (.3, 1.0))
    def test_with_game_keeps_intensity_and_shrinks_turn(self):
        self.assertEqual(app.load_shape(.3, .6), (.6, .5))
        self.assertEqual(app.load_shape(.6, .6), (.6, 1.0))
        self.assertEqual(app.load_shape(0, .6), (.6, 0.0))
    def test_continuous_unit_never_pauses(self):
        compute, stop = RecordingCompute(), RecordingStop()
        app.Pacer().unit(compute, .4, None, stop, 10**9)
        self.assertEqual(compute.calls, [('batch', .4)])
        self.assertEqual(stop.waits, [])
    def test_shared_units_add_up_short_gaps_before_waiting(self):
        compute, stop, pacer = RecordingCompute(), RecordingStop(), app.Pacer()
        for _ in range(2):
            pacer.unit(compute, .3, .6, stop, 10**9)
        self.assertEqual(compute.calls, [('batch', .6), ('drain',)]*2)
        self.assertEqual(stop.waits, [])  # 2 × 6 ms owed: still too short to wait
        pacer.unit(compute, .3, .6, stop, 10**9)
        # Half our turn: the gap owed equals the work done, 3 units = 18 ms.
        self.assertAlmostEqual(stop.waits[0], .018)
    def test_coarse_windows_waits_keep_the_requested_share(self):
        class CoarseStop:
            """Like Windows at its default timer: every wait lasts whole 15.6 ms ticks."""
            def __init__(self):
                self.clock = 0.0
            def wait(self, seconds):
                self.clock += math.ceil(seconds/.0156)*.0156
        stop, compute, pacer = CoarseStop(), RecordingCompute(), app.Pacer()
        def monotonic():
            return stop.clock
        with patch.object(app.time, 'monotonic', monotonic):
            for _ in range(2000):
                pacer.unit(compute, .94*.6, .6, stop, 10**9)
                stop.clock += .006  # the unit itself
        busy = 2000*.006
        self.assertAlmostEqual(busy/stop.clock, .94, delta=.02)
    def test_back_to_continuous_forgets_owed_gap(self):
        compute, stop, pacer = RecordingCompute(), RecordingStop(), app.Pacer()
        pacer.unit(compute, .3, .6, stop, 10**9)
        pacer.unit(compute, .6, .6, stop, 10**9)
        self.assertEqual(pacer.owed, 0.0)
    def test_memory_passes_scale_with_intensity(self):
        obj = app.Compute.__new__(app.Compute)
        obj.sms, obj.vram, obj.vram_bytes, obj.cursor = 84, C.c_uint64(0), 512*1024*1024, 0
        obj.mem_function = None
        grids = []
        obj.launch = lambda *args: grids.append(args[1])
        obj.memory_passes(84)
        self.assertEqual(grids, [84]*16)
        grids.clear()
        obj.memory_passes(1)
        self.assertEqual(grids, [1])
    def test_run_load_keeps_level_only_while_game_is_open(self):
        for names, expected in [({'game.exe'}, .01), (set(), None)]:
            seen = []
            q = queue.Queue()
            def unit(pacer, compute, duty, level, stop, until):
                seen.append(level)
            with patch.object(app, 'Compute', FakeCompute), patch.object(app, 'process_names', return_value=names), patch.object(app.Pacer, 'unit', unit):
                app.run_load(FakeTelemetry(), 0, 100, .05, 75, threading.Event(), q, process='game.exe', sensors=FakeSensors(), trigger=Cutover(ADAPTIVE_MODE, hold=60, grace=60))
            self.assertTrue(seen)
            self.assertTrue(all(level == expected for level in seen), seen[:3])


if __name__ == '__main__':
    unittest.main()
