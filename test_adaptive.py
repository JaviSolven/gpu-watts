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
    def test_game_below_base_but_playing_still_hands_off(self):
        # Base 260 W, the game alone draws ~240 W: under 95 % of the base, yet playing.
        c=AdaptiveHandoff(260,ramp_seconds=5,hold=2,grace=2)
        duty,ready,now=.15,False,0.0
        while not ready and now<30:
            own=duty*300
            duty,ready,text=c.update(now,True,240+own*.2,duty,360,game_watts=207)
            now+=.5
        self.assertTrue(ready)
        self.assertEqual(duty,0)
        self.assertLess(now,12)
        self.assertIn('retirada definitiva',text)
    def test_takeover_is_one_way(self):
        c=AdaptiveHandoff(260,ramp_seconds=5,hold=2,grace=0)
        duty=.15
        for i in range(4):
            duty,_,_=c.update(i*.5,True,230,duty,360,game_watts=200)
        before=duty
        # A loading screen later on no longer brings support back.
        duty,_,_=c.update(2,True,120,duty,360,game_watts=80)
        self.assertLess(duty,before)
    def test_loading_screen_keeps_the_base(self):
        c=AdaptiveHandoff(260,ramp_seconds=5,hold=2,grace=0)
        duty=.6
        for i in range(10):
            duty,ready,_=c.update(i*.5,True,240,duty,360,game_watts=60)
        self.assertEqual(duty,.6)  # still supporting at the level it had
        self.assertFalse(ready)
    def take_over(self, c, duty=.5, start=0.0):
        # The game plays (its part at 80 % of the base) long enough to take over.
        for i in range(4):
            duty,ready,_=c.update(start+i*.5,True,300,duty,360,game_watts=.8*c.target)
        return duty
    def test_watch_brings_support_back_on_a_loading_screen(self):
        c=AdaptiveHandoff(200,ramp_seconds=2,hold=0,grace=0,watch=60)
        duty=self.take_over(c)
        self.assertTrue(c.committed)
        self.assertTrue(c.played)
        for i in range(8):  # menu: the game draws a lot, we go to zero
            duty,ready,_=c.update(2+i*.5,True,250,duty,360,game_watts=230)
        self.assertEqual(duty,0)
        self.assertFalse(ready)  # still watching
        # Loading screen: the game falls under half the base: support comes back.
        for i in range(6):
            duty,ready,text=c.update(6+i*.5,True,90,duty,360,game_watts=80)
        self.assertFalse(c.committed)
        self.assertGreater(duty,0)
        self.assertIn('vigilando',text)
    def test_no_end_while_watching_and_end_after(self):
        c=AdaptiveHandoff(200,ramp_seconds=1,hold=1,grace=0,watch=10)
        duty=self.take_over(c)
        now=2.0
        while now<10:
            duty,ready,_=c.update(now,True,300,duty,360,game_watts=290)
            self.assertFalse(ready)
            now+=.5
        while not ready and now<20:
            duty,ready,_=c.update(now,True,300,duty,360,game_watts=290)
            now+=.5
        self.assertTrue(ready)
        self.assertLess(now,12)
    def test_after_the_watch_a_fall_does_not_bring_support(self):
        c=AdaptiveHandoff(200,ramp_seconds=1,hold=5,grace=0,watch=3)
        duty=self.take_over(c)
        duty,_,_=c.update(4,True,300,duty,360,game_watts=290)
        duty,_,_=c.update(4.5,True,90,duty,360,game_watts=80)
        self.assertTrue(c.committed)
    def test_without_watch_nothing_changes(self):
        c=AdaptiveHandoff(200,ramp_seconds=1,hold=0,grace=0)
        duty=self.take_over(c)
        duty,_,_=c.update(2,True,90,duty,360,game_watts=80)
        self.assertTrue(c.committed)
        self.assertFalse(c.watching(2))
    def test_short_game_peak_is_not_a_takeover(self):
        c=AdaptiveHandoff(260,ramp_seconds=5,hold=2,grace=0)
        duty=.6
        duty,_,_=c.update(0,True,240,duty,360,game_watts=60)
        duty,_,_=c.update(.5,True,240,duty,360,game_watts=200)  # half a second only
        duty,_,_=c.update(1,True,240,duty,360,game_watts=60)
        self.assertFalse(c.committed)


if __name__=='__main__':unittest.main()
