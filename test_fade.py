import queue
import threading
import unittest
from unittest.mock import patch
import gpu_watts as app
from test_control import FakeCompute,FakeSensors,FakeTelemetry


class FadeTests(unittest.TestCase):
    def test_endpoints_and_no_overshoot(self):
        self.assertEqual(app.fade_duty(.7,0,5),.7)
        self.assertEqual(app.fade_duty(.7,5,5),0)
        values=[app.fade_duty(.7,i*.1,5) for i in range(61)]
        self.assertTrue(all(0<=v<=.7 for v in values))
        self.assertTrue(all(a>=b for a,b in zip(values,values[1:])))
    def test_midpoint(self):
        self.assertAlmostEqual(app.fade_duty(.8,2.5,5),.4)
    def test_zero_duration(self):
        self.assertEqual(app.fade_duty(.8,0,0),0)
    def test_manual_stop_skips_all_work(self):
        stop=threading.Event();stop.set()
        obj=FakeCompute('fake')
        with patch.object(obj,'batch') as batch:
            app.ramp_down(obj,.5,5,FakeTelemetry(),None,FakeSensors(),(75,85),stop,queue.Queue())
            batch.assert_not_called()
    def test_thermal_limit_withdraws_instead_of_aborting_fade(self):
        class Hot:
            def read(self):return (80,50)
        obj=FakeCompute('fake')
        q=queue.Queue()
        app.ramp_down(obj,.5,.02,FakeTelemetry(),None,Hot(),(75,85),threading.Event(),q)
        self.assertTrue(any(k=='temperatures' and v[0]==80 for k,v in q.queue))
    def test_timer_finish_runs_fade_then_releases(self):
        q=queue.Queue()
        FakeCompute.instances.clear()
        with patch.object(app,'Compute',FakeCompute):
            app.run_load(FakeTelemetry(),0,100,.02,75,threading.Event(),q,sensors=FakeSensors(),ramp_seconds=.02)
        self.assertTrue(FakeCompute.instances[0].closed)
        self.assertIn(('done','Tiempo completado'),list(q.queue))
        self.assertTrue(any(k=='sample' and 'gradualmente' in v[4] for k,v in q.queue))

    def test_normal_stop_still_executes_fade(self):
        stop=threading.Event()
        class Compute(FakeCompute):
            calls=0
            def batch(self):
                self.__class__.calls+=1
                stop.set()
                return .001
        q=queue.Queue()
        with patch.object(app,'Compute',Compute):
            app.run_load(FakeTelemetry(),0,100,5,75,stop,q,sensors=FakeSensors(),ramp_seconds=.02)
        self.assertGreater(Compute.calls,1)
        self.assertIn(('done','Detenido por el usuario'),list(q.queue))
    def test_sensor_failure_still_withdraws(self):
        class Sensors:
            calls=0
            def read(self):
                self.calls+=1
                if self.calls>1:raise RuntimeError('sensor unavailable')
                return (45,50)
        q=queue.Queue()
        with patch.object(app,'Compute',FakeCompute):
            app.run_load(FakeTelemetry(),0,100,5,75,threading.Event(),q,sensors=Sensors(),ramp_seconds=.02)
        self.assertTrue(any(k=='status' and 'lectura/registro' in v for k,v in q.queue))
    def test_thermal_stop_enters_fade(self):
        class Sensors:
            calls=0
            def read(self):
                self.calls+=1
                return (45,50) if self.calls==1 else (80,50)
        q=queue.Queue()
        with patch.object(app,'Compute',FakeCompute):
            app.run_load(FakeTelemetry(),0,100,5,75,threading.Event(),q,sensors=Sensors(),ramp_seconds=.02)
        self.assertTrue(any(k=='sample' and 'gradualmente' in v[4] for k,v in q.queue))
    def test_cuda_failure_reports_impossible_fade_and_releases(self):
        class Broken(FakeCompute):
            def batch(self):raise RuntimeError('CUDA GPU lost')
        q=queue.Queue()
        with patch.object(app,'Compute',Broken):
            app.run_load(FakeTelemetry(),0,100,5,75,threading.Event(),q,sensors=FakeSensors(),ramp_seconds=.02)
        self.assertTrue(any(k=='done' and 'No se pudo completar' in v for k,v in q.queue))


if __name__=='__main__':unittest.main()
