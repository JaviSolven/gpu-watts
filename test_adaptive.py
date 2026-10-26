import unittest
from cutover import AdaptiveHandoff


class AdaptiveTests(unittest.TestCase):
    def test_no_process_keeps_preload_to_external_regulator(self):
        c=AdaptiveHandoff(200)
        duty,ready,_=c.update(0,False,200,.6,360)
        self.assertEqual(duty,.6)
        self.assertFalse(ready)
    def test_process_appearance_does_not_drop_load(self):
        c=AdaptiveHandoff(200)
        duty,ready,_=c.update(0,True,300,.6,360)
        self.assertEqual(duty,.6)
        self.assertFalse(ready)
    def test_high_power_withdraws_gradually_then_confirms(self):
        c=AdaptiveHandoff(200,ramp_seconds=5,hold=2,grace=0)
        duty=.6
        first_zero=None
        ready=False
        for i in range(21):
            now=i*.5
            previous=duty
            duty,ready,_=c.update(now,True,300,duty,360)
            self.assertLessEqual(previous-duty,.060001)
            if duty==0 and first_zero is None:first_zero=now
            if ready:break
        self.assertIsNotNone(first_zero)
        self.assertGreaterEqual(now-first_zero,2)
        self.assertTrue(ready)
    def test_support_returns_when_game_cannot_sustain_base(self):
        c=AdaptiveHandoff(200,ramp_seconds=1,hold=5,grace=0)
        duty=.5
        for i in range(10):
            duty,_,_=c.update(i*.5,True,350,duty,360)
        self.assertEqual(duty,0)
        duty,ready,_=c.update(5,True,120,duty,360)
        self.assertGreater(duty,0)
        self.assertFalse(ready)
    def test_support_cannot_exceed_pre_game_activity(self):
        c=AdaptiveHandoff(200,ramp_seconds=1)
        c.update(0,True,80,.2,360)
        duty,_,_=c.update(.5,True,40,.2,360)
        self.assertEqual(duty,.2)
    def test_disappearing_process_resets_confirmation(self):
        c=AdaptiveHandoff(200,ramp_seconds=1,hold=2,grace=0)
        c.update(0,True,300,.1,360)
        c.update(1,True,300,.1,360)
        c.update(2,False,300,0,360)
        _,ready,_=c.update(3,True,300,0,360)
        self.assertFalse(ready)
    def test_zero_activity_alone_does_not_confirm_low_power(self):
        c=AdaptiveHandoff(200,hold=0,grace=0)
        _,ready,_=c.update(0,True,100,0,360)
        self.assertFalse(ready)
    def test_slower_smoothing_limits_each_reduction(self):
        c=AdaptiveHandoff(200,ramp_seconds=10,hold=0,grace=0)
        c.update(0,True,350,.8,360)
        duty,_,_=c.update(.5,True,350,.8,360)
        self.assertAlmostEqual(duty,.76)


if __name__=='__main__':unittest.main()
