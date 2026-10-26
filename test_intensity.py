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
        for _ in range(3):
            pacer.unit(compute, .3, .6, stop, 10**9)
        self.assertEqual(compute.calls, [('batch', .6), ('drain',)]*3)
        self.assertEqual(stop.waits, [])  # gaps of 6 ms are added up, not waited one by one
        pacer.unit(compute, .3, .6, stop, 10**9)
        # Half our turn: the gap owed equals the work done, 3 units = 18 ms,
        # and no unit runs while it is owed.
        self.assertAlmostEqual(stop.waits[0], .018)
        self.assertEqual(len(compute.calls), 6)
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
    def test_tiny_turn_is_kept_tiny(self):
        class CoarseStop:
            def __init__(self):
                self.clock = 0.0
            def wait(self, seconds):
                self.clock += math.ceil(seconds/.0156)*.0156
        stop, pacer = CoarseStop(), app.Pacer()
        class Compute(RecordingCompute):
            units = 0
            def batch(self, intensity):
                self.units += 1
                stop.clock += .006
        compute = Compute()
        with patch.object(app.time, 'monotonic', lambda: stop.clock):
            while stop.clock < 60:
                pacer.unit(compute, .012*.8, .8, stop, 10**9)
        # 1.2 % of the time, not the ~6 % floor of one unit per 0.1 s wait.
        self.assertAlmostEqual(compute.units*.006/stop.clock, .012, delta=.003)
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
        # Base 100 W from 40 W idle under a 360 W limit: base intensity (100-40)/(360-40).
        for names, expected in [({'game.exe'}, (100-40)/(360-40)), (set(), None)]:
            seen = []
            q = queue.Queue()
            def unit(pacer, compute, duty, level, stop, until):
                seen.append(level)
            # Not open when INICIAR checks; it appears at the first sample, before the base.
            listing = iter([set()] + [names]*10**6)
            with patch.object(app, 'Compute', FakeCompute), patch.object(app, 'process_names', side_effect=lambda: next(listing)), patch.object(app.Pacer, 'unit', unit):
                app.run_load(FakeTelemetry(), 0, 100, .05, 75, threading.Event(), q, process='game.exe', sensors=FakeSensors(), trigger=Cutover(ADAPTIVE_MODE, hold=60, grace=60))
            self.assertTrue(seen)
            self.assertTrue(all(level == expected for level in seen), seen[:3])
    def test_adaptive_does_not_preload_a_game_already_open(self):
        q = queue.Queue()
        FakeCompute.instances.clear()
        with patch.object(app, 'Compute', FakeCompute), patch.object(app, 'process_names', return_value={'GAME.exe'}):
            app.run_load(FakeTelemetry(), 0, 100, 5, 75, threading.Event(), q, process='game.exe', sensors=FakeSensors(), trigger=Cutover(ADAPTIVE_MODE))
        self.assertFalse(FakeCompute.instances)
        self.assertTrue(any(k == 'done' and 'ya está abierto' in v for k, v in q.queue))
        self.assertNotIn(('autoclose', True), list(q.queue))


if __name__ == '__main__':
    unittest.main()
