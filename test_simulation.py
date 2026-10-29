import queue
import threading
import time
import unittest
from unittest.mock import patch
import gpu_watts as app
import simulation as sim
from cutover import Cutover, ADAPTIVE_MODE


def settled(gpu):
    """Read as if a long time had passed, so the reading reaches its target."""
    gpu.last = time.monotonic() - 100
    gpu.core = 0
    return gpu.read(None)


class SimulationTests(unittest.TestCase):
    def test_idle_without_load_or_game(self):
        watts, _, limit = settled(sim.SimulatedGpu())
        self.assertAlmostEqual(watts, sim.IDLE, delta=2)
        self.assertEqual(limit, sim.LIMIT)
    def test_our_intensity_sets_power(self):
        gpu = sim.SimulatedGpu()
        gpu.units.append((time.monotonic(), sim.WINDOW, .5))
        watts, _, _ = settled(gpu)
        self.assertAlmostEqual(watts, sim.IDLE + .5*(sim.FULL-sim.IDLE), delta=2)
    def test_game_alone_draws_its_watts_after_loading(self):
        gpu = sim.SimulatedGpu()
        gpu.open_game('crimsondesert.exe', 300)
        self.assertAlmostEqual(gpu.game_demand(time.monotonic()), sim.LOADING_DEMAND)
        gpu.game = ('crimsondesert.exe', time.monotonic()-100, 300)
        watts, _, _ = settled(gpu)
        self.assertAlmostEqual(watts, 300, delta=15)
    def test_shared_gpu_time(self):
        gpu = sim.SimulatedGpu()
        gpu.units.append((time.monotonic(), sim.WINDOW, 1))
        gpu.game = ('x.exe', time.monotonic()-100, sim.FULL)
        watts, _, _ = settled(gpu)
        self.assertLessEqual(watts, sim.FULL + 15)
    def test_process_list_follows_the_game(self):
        gpu = sim.SimulatedGpu()
        self.assertEqual(gpu.processes(), set())
        gpu.open_game('steelnomad.exe', 340)
        self.assertEqual(gpu.processes(), {'steelnomad.exe'})
        gpu.close_game()
        self.assertEqual(gpu.processes(), set())
    def test_intensity_in_whole_sms(self):
        gpu = sim.SimulatedGpu()
        gpu.record(sim.UNIT, .004)
        gpu.record(sim.UNIT, 2)
        self.assertEqual([u[2] for u in gpu.units], [1/sim.SMS, 1])
    def test_sensors(self):
        gpu = sim.SimulatedGpu()
        # Like the RTX 5080: no VRAM from the driver, so «NVIDIA directo» fails and core only works.
        self.assertIsNone(gpu.sensors().memory_temperature())
        with self.assertRaisesRegex(RuntimeError, 'VRAM'):
            gpu.sensors().read()
        self.assertIsNone(gpu.sensors(monitor_vram=False).read()[1])
        gpu.vram_by_driver = True  # a card that does give it
        core, memory = gpu.sensors().read()
        self.assertEqual(memory, core+7.5)
    def test_simulated_steam_library(self):
        installed = sim.SimulatedGpu.steam_games()
        names = [name for name, _ in installed]
        self.assertIn('Crimson Desert', names)
        self.assertEqual(names, sorted(names, key=str.casefold))
        folder = dict(installed)['Crimson Desert']
        found = sim.SimulatedGpu.game_executables(folder)
        self.assertEqual([name for name, _, _ in found][0], 'CrimsonDesert.exe')
        self.assertEqual({name for name, _, _ in found[-2:]}, {'EasyAntiCheat_EOS_Setup.exe', 'CrashReporter.exe'})  # helpers last
        self.assertEqual(str(found[0][2].parent.relative_to(folder)), 'bin64')
        launcher = sim.SimulatedGpu.game_executables(dict(installed)['Juego de ejemplo con lanzador'])
        self.assertEqual(launcher[0][0], 'JuegoEjemplo-Win64-Shipping.exe')
        self.assertEqual(sim.SimulatedGpu.game_executables(sim.STEAM_ROOT / 'otro'), [])
    def test_game_opened_before_the_base_still_hands_off(self):
        # The case from the screenshot: base 300 W, game 320 W, opened right after INICIAR.
        gpu, q = sim.SimulatedGpu(), queue.Queue()
        gpu.open_game('steelnomad.exe', 320)
        listing = iter([set()] + [{'steelnomad.exe'}]*10**6)
        with patch.object(sim, 'LOADING', .5):
            app.run_load(gpu, 0, 300, 12, 75, threading.Event(), q, process='steelnomad.exe', sensors=gpu.sensors(monitor_vram=False),
                         trigger=Cutover(ADAPTIVE_MODE, hold=1, grace=0), ramp_seconds=1,
                         make_compute=gpu.compute, processes=lambda: next(listing))
        self.assertTrue(any(k == 'done' and v.startswith('Relevo:') for k, v in q.queue), [v for k, v in q.queue if k == 'done'])
        self.assertIn(('autoclose', True), list(q.queue))
        self.assertEqual([v for k, v in q.queue if k == 'phase'], ['relevo'])
    def test_split_between_us_and_the_game(self):
        gpu, q = sim.SimulatedGpu(), queue.Queue()
        def processes():
            # The player opens the game when the green notice appears.
            if gpu.game is None and ('phase', 'base') in list(q.queue):
                gpu.open_game('steelnomad.exe', 320)
            return gpu.processes()
        with patch.object(sim, 'LOADING', .5):
            app.run_load(gpu, 0, 200, 20, 75, threading.Event(), q, process='steelnomad.exe', sensors=gpu.sensors(monitor_vram=False),
                         trigger=Cutover(ADAPTIVE_MODE, hold=.5, grace=0), ramp_seconds=1,
                         make_compute=gpu.compute, processes=processes)
        events = list(q.queue)
        self.assertIn(('autoclose', True), events)
        splits = [v for k, v in events if k == 'split' and v is not None]
        totals = [v[0] for k, v in events if k == 'sample']
        self.assertTrue(splits)
        own_first, own_last = splits[0][0], splits[-1][0]
        # At first almost everything above idle (~35 W) is ours; at the end nothing is.
        self.assertGreater(own_first, 120)
        self.assertLess(own_first, 210)
        self.assertLess(own_last, 5)
        for own, rest in splits:
            self.assertGreaterEqual(own, 0)
            self.assertGreater(rest, 0)
        # Each split adds up to that sample's board reading (the last one comes with the handoff).
        for own, rest in splits[:-1]:
            self.assertTrue(any(abs(own+rest-total) < .01 for total in totals))
    def test_game_below_the_base_hands_off_once_playing(self):
        # The case from the second screenshot: base 260 W, game 240 W.
        gpu, q = sim.SimulatedGpu(), queue.Queue()
        def processes():
            if gpu.game is None and ('phase', 'base') in list(q.queue):
                gpu.open_game('steelnomad.exe', 240)
            return gpu.processes()
        with patch.object(sim, 'LOADING', .5):
            app.run_load(gpu, 0, 260, 30, 75, threading.Event(), q, process='steelnomad.exe', sensors=gpu.sensors(monitor_vram=False),
                         trigger=Cutover(ADAPTIVE_MODE, hold=1, grace=0), ramp_seconds=2,
                         make_compute=gpu.compute, processes=processes)
        self.assertTrue(any(k == 'done' and v.startswith('Relevo:') for k, v in q.queue), [v for k, v in q.queue if k == 'done'])
        self.assertIn(('autoclose', True), list(q.queue))
    def test_run_load_reaches_towards_the_base(self):
        gpu, q = sim.SimulatedGpu(), queue.Queue()
        app.run_load(gpu, 0, 100, 3, 75, threading.Event(), q, sensors=gpu.sensors(monitor_vram=False), make_compute=gpu.compute, processes=gpu.processes)
        watts = [v[0] for k, v in q.queue if k == 'sample']
        self.assertIn(('done', 'Tiempo completado'), list(q.queue))
        # The notice goes from "wait" to "open the game now", once each.
        self.assertEqual([v for k, v in q.queue if k == 'phase'], ['subiendo', 'base'])
        self.assertGreater(max(watts), 100 - 10)
        self.assertLess(max(watts), 100 + 30)


if __name__ == '__main__':
    unittest.main()
