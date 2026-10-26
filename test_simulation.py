import queue
import threading
import time
import unittest
import gpu_watts as app
import simulation as sim


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
        core, memory = gpu.sensors().read()
        self.assertEqual(memory, core+7.5)
        self.assertIsNone(gpu.sensors(monitor_vram=False).read()[1])
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
    def test_run_load_reaches_towards_the_base(self):
        gpu, q = sim.SimulatedGpu(), queue.Queue()
        app.run_load(gpu, 0, 150, 3, 75, threading.Event(), q, sensors=gpu.sensors(), make_compute=gpu.compute, processes=gpu.processes)
        watts = [v[0] for k, v in q.queue if k == 'sample']
        self.assertIn(('done', 'Tiempo completado'), list(q.queue))
        self.assertGreater(max(watts), sim.IDLE + 40)
        self.assertLess(max(watts), 150 + 30)


if __name__ == '__main__':
    unittest.main()
