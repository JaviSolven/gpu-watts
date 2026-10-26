import queue
import threading
import unittest
from unittest.mock import patch
import gpu_watts as app
from cutover import Cutover
from test_control import FakeCompute, FakeSensors


class CutoverTests(unittest.TestCase):
    def test_gpu_requires_process(self):
        c=Cutover(grace=0,hold=0)
        self.assertFalse(c.update(0,False,300))
        self.assertTrue(c.update(1,True,180))
    def test_opening_alone_does_not_trigger(self):
        c=Cutover(grace=0,hold=0)
        self.assertFalse(c.update(0,True,100))
    def test_grace_and_sustained_threshold(self):
        c=Cutover(grace=2,hold=2)
        for now in (0,1,2,3):
            self.assertFalse(c.update(now,True,180))
        self.assertTrue(c.update(4,True,180))
    def test_transient_resets_confirmation(self):
        c=Cutover(grace=0,hold=2)
        self.assertFalse(c.update(0,True,180))
        self.assertFalse(c.update(1,True,100))
        self.assertFalse(c.update(2,True,180))
        self.assertFalse(c.update(3,True,180))
        self.assertTrue(c.update(4,True,180))
    def test_process_exit_resets_state(self):
        c=Cutover(grace=0,hold=2)
        c.update(0,True,180)
        c.update(1,False,180)
        self.assertFalse(c.update(2,True,180))
    def test_cpu_is_normalized_and_requires_two_samples(self):
        c=Cutover(mode='CPU del proceso supera %',cpu=20,grace=0,hold=0,processors=4)
        self.assertFalse(c.update(0,True,40,{(1,1):10}))
        self.assertTrue(c.update(1,True,40,{(1,1):11}))
        self.assertEqual(c.cpu_percent,25)
    def test_pid_reuse_is_not_counted(self):
        c=Cutover(mode='CPU del proceso supera %',cpu=10,grace=0,hold=0,processors=4)
        c.update(0,True,40,{(1,1):10})
        self.assertFalse(c.update(1,True,40,{(1,2):100}))
        self.assertEqual(c.cpu_percent,0)
    def test_gpu_or_cpu_and_combination(self):
        for mode,expected in [('GPU o CPU',True),('GPU y CPU',False)]:
            c=Cutover(mode=mode,grace=0,hold=0,processors=4)
            self.assertEqual(c.update(0,True,180,{(1,1):0}),expected)
    def test_invalid_configuration(self):
        for kwargs in ({'watts':float('nan')},{'cpu':0},{'hold':-1},{'mode':'unknown'}):
            with self.assertRaises(ValueError): Cutover(**kwargs)
    def test_already_open_process_waits_for_gpu_readiness(self):
        class Telemetry:
            devices=[(None,'Fake','GPU-fake')]
            def __init__(self): self.calls=0
            def read(self,_):
                self.calls+=1
                return ((40 if self.calls==1 else 180),45,360)
        q=queue.Queue()
        FakeCompute.instances.clear()
        with patch.object(app,'Compute',FakeCompute),patch.object(app,'process_names',return_value={'game.exe'}):
            app.run_load(Telemetry(),0,100,2,75,threading.Event(),q,process='game.exe',sensors=FakeSensors(),trigger=Cutover(grace=0,hold=0))
        self.assertTrue(FakeCompute.instances[0].closed)
        self.assertIn(('autoclose',True),list(q.queue))
        self.assertTrue(any(k=='done' and v.startswith('Relevo:') for k,v in q.queue))


if __name__=='__main__': unittest.main()
