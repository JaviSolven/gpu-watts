import queue
import threading
import unittest
from unittest.mock import patch
import gpu_watts as app


class FakeTelemetry:
    devices = [(None, 'Fake GPU', 'GPU-fake')]
    def read(self, _):
        return (40, 45, 360)


class FakeCompute:
    instances = []
    def __init__(self, uuid, mode='Core', vram_mb=512):
        self.closed = False
        self.__class__.instances.append(self)
    def batch(self, intensity):
        pass
    def drain(self):
        return 0.001
    def close(self):
        self.closed = True


class FakeSensors:
    def read(self):
        return (45, 50)


class ControlTests(unittest.TestCase):
    def setUp(self):
        FakeCompute.instances.clear()
    def test_feedback_bounded_and_direction(self):
        self.assertGreater(app.next_duty(.2, 100, 50, 360), .2)
        self.assertLess(app.next_duty(.2, 100, 150, 360), .2)
        self.assertEqual(app.next_duty(.2, 100, 101, 360), .2)
        for value in (0, .01, .5, 1):
            for measured in (0, 100, 2000):
                result = app.next_duty(value, 100, measured, 360)
                self.assertTrue(0 <= result <= 1)
                self.assertLessEqual(result-value, .080001)
    def test_persistent_deficit_increases_activity_within_ten_seconds(self):
        duty = .01
        for _ in range(20):
            duty = app.next_duty(duty,100,50,360)
        self.assertEqual(duty,1.0)

    def test_validation(self):
        for values in [(float('nan'), 10, 75), (100, 0, 75), (100, 5, 90), (float('inf'), 10, 75)]:
            with self.assertRaises(ValueError):
                app.validate(*values)
        app.validate(100, 10, 75)
        self.assertEqual(app.normalized_process('Nomad'), 'nomad.exe')
        self.assertEqual(app.normalized_process('TEST.EXE'), 'test.exe')
        with self.assertRaises(ValueError):
            app.normalized_process('C:\\test.exe')
    def execute(self, process='', reads=None, duration=.05, stop=None):
        q = queue.Queue()
        with patch.object(app, 'Compute', FakeCompute), patch.object(app, 'process_names', side_effect=reads):
            app.run_load(FakeTelemetry(), 0, 100, duration, 75, stop or threading.Event(), q, process, True, FakeSensors())
        return list(q.queue)
    def test_process_already_open_does_not_load(self):
        events = self.execute('nomad.exe', [{'NOMAD.exe'}])
        self.assertFalse(FakeCompute.instances)
        self.assertIn(('autoclose', True), events)
    def test_process_appearing_stops_and_releases(self):
        events = self.execute('nomad.exe', [set(), {'Nomad.exe'}])
        self.assertTrue(FakeCompute.instances[0].closed)
        self.assertIn(('autoclose', True), events)
    def test_timer_releases(self):
        events = self.execute()
        self.assertTrue(FakeCompute.instances[0].closed)
        self.assertIn(('done', 'Tiempo completado'), events)
    def test_cancel_before_loading(self):
        stop = threading.Event()
        stop.set()
        self.execute(stop=stop)
        self.assertFalse(FakeCompute.instances)
    def test_process_enumeration_failure_stops(self):
        events = self.execute('nomad.exe', [set(), OSError('enumeration failed')])
        self.assertTrue(FakeCompute.instances[0].closed)
        self.assertTrue(any(k == 'done' and 'Error:' in v for k, v in events))
    def test_limit_and_temperature_refused(self):
        for target, cutoff in [(400,75), (100,40)]:
            q = queue.Queue()
            with patch.object(app, 'Compute', FakeCompute):
                app.run_load(FakeTelemetry(), 0, target, 1, cutoff, threading.Event(), q, sensors=FakeSensors())
            self.assertFalse(FakeCompute.instances)
            self.assertTrue(any(k == 'done' and 'Error:' in v for k, v in list(q.queue)))


if __name__ == '__main__':
    unittest.main()
