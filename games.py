"""Saved games with their own settings, and the installed Steam library."""
import json
import os
from pathlib import Path
import re

try:
    import winreg
except ImportError:  # only exists on Windows
    winreg = None


def default_path():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'GPU_Watts' / 'juegos.json'


class GameStore:
    """Games used before, keyed by executable name, most used first."""
    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        self.games, self.last = {}, None
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            for exe, game in data['games'].items():
                self.games[exe] = {'name': str(game['name']), 'uses': int(game.get('uses', 0)),
                                   'settings': dict(game.get('settings', {}))}
            if data.get('last') in self.games:
                self.last = data['last']
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # Unreadable file: keep a copy instead of overwriting it on the next save.
            self.games, self.last = {}, None
            try:
                self.path.replace(self.path.with_name(self.path.name + '.danado'))
            except OSError:
                pass

    def ordered(self):
        return sorted(self.games, key=lambda exe: (-self.games[exe]['uses'], self.games[exe]['name'].casefold()))

    def add(self, exe, name):
        """Save a game picked in advance (Steam), without counting a use."""
        game = self.games.setdefault(exe, {'name': name, 'uses': 0, 'settings': {}})
        game['name'] = name
        self.last = exe
        self.save()

    def remember(self, exe, settings):
        """Count a use and keep the settings it was started with."""
        game = self.games.setdefault(exe, {'name': exe, 'uses': 0, 'settings': {}})
        game['uses'] += 1
        game['settings'] = dict(settings)
        self.last = exe
        self.save()

    def forget(self, exe):
        self.games.pop(exe, None)
        if self.last == exe:
            self.last = None
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(json.dumps({'games': self.games, 'last': self.last}, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.path)


# Steam keeps its library in Valve KeyValues text files: "key"  "value" pairs.
PAIR = re.compile(r'"([^"]*)"\s+"((?:[^"\\]|\\.)*)"')
COMMON_REDISTRIBUTABLES = '228980'
# Executables that are not the game itself; still listed, but last.
HELPERS = re.compile(r'crash|report|redist|setup|install|unins|dxsetup|directx|easyanticheat|battleye|beservice|prereq|dotnet|update|cefprocess|webhelper', re.I)


def pairs(text):
    return [(key.lower(), re.sub(r'\\(.)', r'\1', value)) for key, value in PAIR.findall(text)]


def steam_root():
    if winreg is None:
        return None
    for hive, key, value in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                             (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath')):
        try:
            with winreg.OpenKey(hive, key) as handle:
                return Path(winreg.QueryValueEx(handle, value)[0])
        except OSError:
            continue
    return None


def steam_libraries(root):
    folders = [Path(root)]
    try:
        text = (Path(root) / 'steamapps' / 'libraryfolders.vdf').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return folders
    found = pairs(text)
    # Current format has "path" keys; the old one numbered the folders directly.
    paths = [v for k, v in found if k == 'path'] or [v for k, v in found if k.isdigit() and ':' in v]
    for path in paths:
        if all(os.path.normcase(os.path.normpath(path)) != os.path.normcase(os.path.normpath(f)) for f in folders):
            folders.append(Path(path))
    return folders


def steam_games(root=None):
    """Installed Steam games as (name, folder), by name."""
    root = root or steam_root()
    if root is None:
        return []
    games = {}
    for library in steam_libraries(root):
        for manifest in (library / 'steamapps').glob('appmanifest_*.acf'):
            try:
                text = manifest.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            values = {}
            for key, value in pairs(text):
                values.setdefault(key, value)  # top-level keys come first
            folder = library / 'steamapps' / 'common' / values.get('installdir', '')
            if values.get('appid') == COMMON_REDISTRIBUTABLES or not values.get('installdir') or not folder.is_dir():
                continue
            games[os.path.normcase(str(folder))] = (values.get('name') or values['installdir'], folder)
    return sorted(games.values(), key=lambda game: game[0].casefold())


def game_executables(folder, depth=4):
    """Executables in a game folder as (name, bytes, path): likely game first, helpers last."""
    folder = Path(folder)
    found = []
    for current, dirs, files in os.walk(folder):
        if len(Path(current).relative_to(folder).parts) >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not HELPERS.search(d)]
        for name in files:
            if name.lower().endswith('.exe'):
                path = Path(current) / name
                try:
                    found.append((name, path.stat().st_size, path))
                except OSError:
                    continue
    # The game is usually the largest executable that is not a helper.
    return sorted(found, key=lambda exe: (bool(HELPERS.search(exe[0])), -exe[1], exe[0].casefold()))
