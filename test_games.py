import json
from pathlib import Path
import tempfile
import unittest
import games


LIBRARY_VDF = '''"libraryfolders"
{
	"0"
	{
		"path"		"%s"
		"apps"
		{
			"228980"		"0"
		}
	}
	"1"
	{
		"path"		"%s"
	}
}
'''

MANIFEST = '''"AppState"
{
	"appid"		"%s"
	"name"		"%s"
	"installdir"		"%s"
	"UserConfig"
	{
		"language"		"spanish"
	}
}
'''


def write(path, text='', size=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    if size:
        path.write_bytes(b'\0' * size)
    else:
        path.write_text(text, encoding='utf-8')


class GameStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'GPU_Watts' / 'juegos.json'
    def tearDown(self):
        self.folder.cleanup()
    def test_remember_survives_restart_and_orders_by_use(self):
        store = games.GameStore(self.path)
        store.add('crimsondesert.exe', 'Crimson Desert')
        store.remember('steelnomad.exe', {'fields': ['200', '120', '75', '85']})
        store.remember('steelnomad.exe', {'fields': ['180', '120', '75', '85']})
        again = games.GameStore(self.path)
        self.assertEqual(again.ordered(), ['steelnomad.exe', 'crimsondesert.exe'])
        self.assertEqual(again.games['steelnomad.exe']['uses'], 2)
        self.assertEqual(again.games['steelnomad.exe']['settings']['fields'][0], '180')
        self.assertEqual(again.last, 'steelnomad.exe')
    def test_steam_name_is_kept_when_used(self):
        store = games.GameStore(self.path)
        store.add('crimsondesert.exe', 'Crimson Desert')
        store.remember('crimsondesert.exe', {'mode': 'Core'})
        self.assertEqual(games.GameStore(self.path).games['crimsondesert.exe']['name'], 'Crimson Desert')
    def test_forget_clears_last(self):
        store = games.GameStore(self.path)
        store.remember('steelnomad.exe', {})
        store.forget('steelnomad.exe')
        again = games.GameStore(self.path)
        self.assertEqual(again.games, {})
        self.assertIsNone(again.last)
    def test_missing_file_is_empty(self):
        self.assertEqual(games.GameStore(self.path).ordered(), [])
    def test_damaged_file_is_kept_aside_not_overwritten(self):
        write(self.path, '{esto no es json')
        store = games.GameStore(self.path)
        self.assertEqual(store.games, {})
        self.assertTrue(self.path.with_name('juegos.json.danado').exists())
        store.remember('steelnomad.exe', {})
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['last'], 'steelnomad.exe')


class SteamTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.root, self.second = base / 'Steam', base / 'D' / 'SteamLibrary'
        escaped = lambda p: str(p).replace('\\', '\\\\')
        write(self.root / 'steamapps' / 'libraryfolders.vdf', LIBRARY_VDF % (escaped(self.root), escaped(self.second)))
        write(self.root / 'steamapps' / 'appmanifest_228980.acf', MANIFEST % ('228980', 'Steamworks Common Redistributables', 'Steamworks Shared'))
        write(self.root / 'steamapps' / 'common' / 'Steamworks Shared' / 'x.exe', size=10)
        write(self.second / 'steamapps' / 'appmanifest_1.acf', MANIFEST % ('1', 'Crimson Desert', 'Crimson Desert'))
        write(self.second / 'steamapps' / 'appmanifest_2.acf', MANIFEST % ('2', '3DMark Steel Nomad', 'Steel Nomad'))
        write(self.second / 'steamapps' / 'appmanifest_3.acf', MANIFEST % ('3', 'Uninstalled', 'Gone'))
        game = self.second / 'steamapps' / 'common' / 'Crimson Desert'
        write(game / 'bin64' / 'CrimsonDesert.exe', size=5000)
        write(game / 'CrashReporter.exe', size=9000)
        write(game / '_CommonRedist' / 'vcredist' / 'vc_redist.x64.exe', size=20000)
        write(game / 'Launcher.exe', size=300)
        write(self.second / 'steamapps' / 'common' / 'Steel Nomad' / 'SteelNomad.exe', size=100)
    def tearDown(self):
        self.folder.cleanup()
    def test_lists_games_in_every_library(self):
        names = [name for name, _ in games.steam_games(self.root)]
        self.assertEqual(names, ['3DMark Steel Nomad', 'Crimson Desert'])
    def test_largest_non_helper_executable_first(self):
        folder = dict(games.steam_games(self.root))['Crimson Desert']
        found = [name for name, _, _ in games.game_executables(folder)]
        self.assertEqual(found, ['CrimsonDesert.exe', 'Launcher.exe', 'CrashReporter.exe'])
    def test_old_numbered_library_format(self):
        write(self.root / 'steamapps' / 'libraryfolders.vdf', '"LibraryFolders"\n{\n\t"TimeNextStatsReport"\t\t"1"\n\t"1"\t\t"%s"\n}\n' % str(self.second).replace('\\', '\\\\'))
        self.assertEqual(games.steam_libraries(self.root), [self.root, self.second])
    def test_no_steam(self):
        self.assertEqual(games.steam_games(Path(self.folder.name) / 'nothing'), [])


if __name__ == '__main__':
    unittest.main()
