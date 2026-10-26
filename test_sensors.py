import ctypes as C
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import gpu_watts as app
from sensors import CsvSensors, NativeSensors, Field, check_temperatures
from test_control import FakeCompute, FakeTelemetry


class SensorTests(unittest.TestCase):
    def test_each_cutoff_stops(self):
        for values in ((76, 60), (50, 86)):
            with self.assertRaises(RuntimeError):
                check_temperatures(values, (75, 85))
    def test_missing_invalid_sensors_refused(self):
        for values in ((50, None), (50, float('nan')), (0, 60), (50,)):
            with self.assertRaises(RuntimeError):
                check_temperatures(values)
    def test_csv_mapping_decimals_and_partial_row(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'sensors.csv'
            p.write_text('Other;Core;Hot;Memory\n999;45,5;60;65\n999;46;', encoding='utf-8')
            reader = CsvSensors(p, (1,3))
            self.assertEqual(reader.read(), (45.5,65))
            with self.assertRaises(ValueError):
                CsvSensors(p, (1,1))
            os.utime(p, (time.time()-10, time.time()-10))
            with self.assertRaises(RuntimeError):
                reader.read()
    def test_native_field_layout_and_types(self):
        self.assertEqual(C.sizeof(Field), 40)
        class Fields:
            def __call__(self, h, count, ptr):
                f = C.cast(ptr, C.POINTER(Field)).contents
                self.field_id = f.fieldId
                f.nvmlReturn, f.valueType = 0, 1
                f.value.uiVal = 65
                f.timestamp = int(time.time()*1e6)
                return 0
        class Lib:
            nvmlDeviceGetFieldValues = Fields()
        self_test = self
        class Telemetry:
            lib = Lib()
            def temp(self, h, sensor, ptr):
                self_test.assertEqual(sensor, 0, 'Must never query hotspot')
                C.cast(ptr,C.POINTER(C.c_uint)).contents.value = 45
        sensors = NativeSensors(Telemetry(), None)
        self.assertEqual(sensors.read(), (45,65))
        self.assertEqual(sensors.getfields.field_id, 82)
    def test_vram_lost_midrun_releases(self):
        class Sensors:
            def __init__(self): self.calls = 0
            def read(self):
                self.calls += 1
                return (45,50) if self.calls == 1 else (45,None)
        FakeCompute.instances.clear()
        q = queue.Queue()
        with patch.object(app,'Compute',FakeCompute):
            app.run_load(FakeTelemetry(),0,100,2,75,threading.Event(),q,sensors=Sensors())
        self.assertTrue(FakeCompute.instances[0].closed)
        self.assertTrue(any(k=='done' and 'VRAM' in v for k,v in q.queue))
    def test_core_only_never_queries_memory_or_hotspot(self):
        owner = self
        class Fields:
            def __call__(self, *_):
                raise AssertionError('Memory must not be queried')
        class Lib:
            nvmlDeviceGetFieldValues = Fields()
        class Telemetry:
            lib = Lib()
            def temp(self, h, sensor, ptr):
                owner.assertEqual(sensor, 0)
                C.cast(ptr,C.POINTER(C.c_uint)).contents.value = 45
        sensors = NativeSensors(Telemetry(), None, monitor_vram=False)
        self.assertEqual(sensors.read(), (45, None))
    def test_geforce_without_memory_temperature_says_what_to_choose(self):
        class Fields:
            def __call__(self, *_):
                return 3  # NVML_ERROR_NOT_SUPPORTED, usual on GeForce
        class Lib:
            nvmlDeviceGetFieldValues = Fields()
        class Telemetry:
            lib = Lib()
            def temp(self, h, sensor, ptr):
                C.cast(ptr,C.POINTER(C.c_uint)).contents.value = 45
        with self.assertRaisesRegex(RuntimeError, 'NVIDIA solo core'):
            NativeSensors(Telemetry(), None).read()
    def test_missing_memory_requires_explicit_opt_out(self):
        with self.assertRaises(RuntimeError):
            check_temperatures((45,None), (75,85))
        self.assertEqual(check_temperatures((45,None), (75,85), require_memory=False), (45,None))
        with self.assertRaises(RuntimeError):
            check_temperatures((None,None), (75,85), require_memory=False)
        with self.assertRaises(RuntimeError):
            check_temperatures((80,None), (75,85), require_memory=False)
    def test_core_only_run_completes_and_releases(self):
        class Sensors:
            monitor_vram = False
            def read(self): return (45,None)
        FakeCompute.instances.clear()
        q = queue.Queue()
        with patch.object(app,'Compute',FakeCompute):
            app.run_load(FakeTelemetry(),0,100,.02,75,threading.Event(),q,sensors=Sensors())
        self.assertTrue(FakeCompute.instances[0].closed)
        self.assertIn(('done','Tiempo completado'),list(q.queue))

    def fake_compute(self, mode='Core', sms=84):
        obj = app.Compute.__new__(app.Compute)
        obj.mode, obj.sms, obj.loops = mode, sms, 64
        obj.slots = [(C.c_void_p(1),C.c_void_p(2),C.c_void_p(3)), (C.c_void_p(4),C.c_void_p(5),C.c_void_p(6))]
        obj.turn, obj.pending, obj.launched = 0, None, False
        obj.memory, obj.function = C.c_uint64(0), None
        obj.record_event = lambda *_: None
        obj.launches = []
        obj.launch = lambda *args: obj.launches.append(args)
        return obj
    def test_cuda_event_time_excludes_host_wait(self):
        obj = self.fake_compute()
        obj.wait_event = lambda _: None
        def measured(ptr, *_):
            C.cast(ptr,C.POINTER(C.c_float)).contents.value = 1.0
        obj.elapsed_event = measured
        with patch.object(app.time,'monotonic',side_effect=[0.0,.020]):
            self.assertAlmostEqual(obj.finish(obj.slots[0],64),.001)
    def test_core_calibration_uses_launch_loops_and_device_duration(self):
        obj = self.fake_compute()
        obj.loops = 999  # already changed for the queued unit; must not compound
        obj.wait_event = lambda _: None
        obj.elapsed = lambda *_: .001
        obj.finish(obj.slots[0],64)
        self.assertEqual(obj.loops,128)
    def test_intensity_sets_busy_sms_not_bursts(self):
        obj = self.fake_compute()
        obj.finish = lambda *_: .001
        for intensity,blocks in [(1,84),(.5,42),(.004,1),(0,1),(2,84)]:
            obj.launches.clear()
            obj.batch(intensity)
            grid, threads = obj.launches[0][1], obj.launches[0][4]
            self.assertEqual((grid,threads),(blocks,app.THREADS))
    def test_next_unit_is_queued_before_waiting(self):
        obj = self.fake_compute()
        waited = []
        obj.finish = lambda slot,loops: waited.append((slot,len(obj.launches)))
        obj.batch(.5)
        self.assertEqual(waited,[])
        obj.batch(.5)
        # Waits for the first unit only after launching the second one.
        self.assertEqual(waited,[(obj.slots[0],2)])
        self.assertEqual(obj.pending[0],obj.slots[1])
        obj.drain()
        self.assertEqual(waited[-1][0],obj.slots[1])
        self.assertIsNone(obj.pending)
        self.assertEqual(obj.drain(),0.0)
    def test_close_waits_for_queued_unit(self):
        obj = self.fake_compute()
        calls = []
        def broken(*args):
            raise RuntimeError('launch failed')
        obj.launch = broken
        with self.assertRaises(RuntimeError):
            obj.batch(.5)  # failed half-way: nothing pending, but work may be queued
        obj.sync = lambda: calls.append('sync')
        obj.destroy_event = lambda e: calls.append('event')
        obj.free = lambda _: calls.append('free')
        obj.unload = lambda _: calls.append('unload')
        obj.vram, obj.mem_module, obj.module = C.c_uint64(0), C.c_void_p(), C.c_void_p()
        obj.memory = C.c_uint64(7)
        obj.retained = False
        obj.close()
        self.assertEqual(calls[0],'sync')
        self.assertLess(calls.index('sync'),calls.index('free'))

    def test_memory_chunk_bounds_and_wrap(self):
        obj = self.fake_compute('VRAM')
        obj.vram = C.c_uint64(0x100000)
        obj.mem_function = None
        obj.vram_bytes = 64*1024*1024
        obj.cursor = 0
        calls = []
        def launch(*args):
            kernel_args = args[9]
            pointer = C.cast(kernel_args[0],C.POINTER(C.c_uint64)).contents.value
            count = C.cast(kernel_args[1],C.POINTER(C.c_uint)).contents.value
            calls.append((pointer,count))
        obj.launch = launch
        obj.finish = lambda *_: .001
        for _ in range(4): obj.batch(1)
        self.assertEqual(len(calls),64)
        self.assertEqual(obj.cursor,0)
        self.assertEqual(len(set(p for p,_ in calls)),4)
        for pointer,count in calls:
            self.assertGreaterEqual(pointer,obj.vram.value)
            self.assertLessEqual(pointer+count*4,obj.vram.value+obj.vram_bytes)


if __name__=='__main__':
    unittest.main()
