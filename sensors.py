"""Core and VRAM monitoring. No estimation of missing temperatures."""
import csv
import ctypes as C
import math
from pathlib import Path
import time


class Value(C.Union):
    _fields_ = [('dVal', C.c_double), ('uiVal', C.c_uint),
                ('ulVal', C.c_ulong), ('ullVal', C.c_ulonglong), ('sllVal', C.c_longlong), ('siVal', C.c_int), ('usVal', C.c_ushort)]


class Field(C.Structure):
    _fields_ = [('fieldId', C.c_uint), ('scopeId', C.c_uint),
                ('timestamp', C.c_longlong), ('latencyUsec', C.c_longlong),
                ('valueType', C.c_int), ('nvmlReturn', C.c_int), ('value', Value)]


def check_temperatures(values, cutoffs=None, require_memory=True):
    if len(values) != 2:
        raise RuntimeError('Se requieren dos posiciones: core y VRAM.')
    labels = ('Core', 'VRAM')
    for i, (label, value) in enumerate(zip(labels, values)):
        if i == 1 and not require_memory and value is None:
            continue
        if value is None or not math.isfinite(value) or not 1 <= value <= 125:
            raise RuntimeError(f'{label}: sensor no disponible o lectura inválida. Usa CSV HWiNFO si procede.')
        if cutoffs is not None and value >= cutoffs[i]:
            raise RuntimeError(f'Parada por {label}: {value:.1f} °C (límite {cutoffs[i]:.0f} °C)')
    if len(values) != 2:
        raise RuntimeError('Se requieren dos sensores: core y VRAM.')
    return tuple(values)


class NativeSensors:
    def __init__(self, telemetry, handle, monitor_vram=True):
        self.telemetry, self.handle = telemetry, handle
        self.monitor_vram = monitor_vram
        self.getfields = getattr(telemetry.lib, 'nvmlDeviceGetFieldValues', None)
        if self.getfields is not None:
            self.getfields.argtypes = [C.c_void_p, C.c_int, C.POINTER(Field)]
            self.getfields.restype = C.c_int

    def read(self):
        temps = []
        for sensor in (0,):  # Core only. Hotspot is deliberately never queried.
            value = C.c_uint()
            try:
                self.telemetry.temp(self.handle, sensor, C.byref(value))
                temps.append(value.value)
            except RuntimeError:
                temps.append(None)
        memory = None
        if self.monitor_vram and self.getfields:
            field = Field()
            field.fieldId = 82  # NVML_FI_DEV_MEMORY_TEMP
            if self.getfields(self.handle, 1, C.byref(field)) == 0 and field.nvmlReturn == 0:
                names = {0:'dVal', 1:'uiVal', 2:'ulVal', 3:'ullVal', 4:'sllVal', 5:'siVal', 6:'usVal'}
                if field.valueType in names:
                    memory = float(getattr(field.value, names[field.valueType]))
                # Reject a stale cached field when NVML supplies a timestamp.
                if field.timestamp <= 0 or abs(time.time() - field.timestamp/1e6) > 5:
                    memory = None
        return check_temperatures((*temps, memory), require_memory=self.monitor_vram)


class CsvSensors:
    """Read an actively appended HWiNFO CSV. Columns are explicitly selected by index."""
    def __init__(self, path, columns):
        self.path = Path(path)
        self.columns = tuple(columns)
        self.encoding, self.delimiter, self.headers = self.inspect(self.path)
        if len(self.columns) != 2 or len(set(self.columns)) != 2 or any(i < 0 or i >= len(self.headers) for i in self.columns):
            raise ValueError('Selecciona dos columnas diferentes: core y VRAM de la GPU elegida.')
        self.last_size = None
        self.last_change = time.monotonic()

    @staticmethod
    def inspect(path):
        with Path(path).open('rb') as f:
            raw = f.readline()
        try:
            text = raw.decode('utf-8-sig')
            encoding = 'utf-8-sig'
        except UnicodeDecodeError:
            text, encoding = raw.decode('cp1252'), 'cp1252'
        delim = ';' if text.count(';') > text.count(',') else ','
        headers = next(csv.reader([text], delimiter=delim))
        if len(headers) < 2:
            raise ValueError('CSV no compatible: se esperan al menos dos columnas.')
        return encoding, delim, headers

    def read(self):
        stat = self.path.stat()
        if time.time()-stat.st_mtime > 5 or stat.st_mtime-time.time() > 5:
            raise RuntimeError('CSV detenido o antiguo (>5 s). Activa el registro continuo a 0,5–1 s.')
        if stat.st_size != self.last_size:
            self.last_size, self.last_change = stat.st_size, time.monotonic()
        elif time.monotonic()-self.last_change > 5:
            raise RuntimeError('El CSV no recibe nuevas filas (>5 s).')
        with self.path.open('rb') as f:
            start = max(0, stat.st_size - 262144)
            f.seek(start)
            raw = f.read()
        # Only complete rows; never accept a partially written latest sample.
        lines = raw.splitlines(keepends=True)
        if start and lines:
            lines.pop(0)
        if lines and not lines[-1].endswith((b'\n', b'\r')):
            lines.pop()
        for line in reversed(lines):
            if not line.strip():
                continue
            row = next(csv.reader([line.decode(self.encoding)], delimiter=self.delimiter))
            if len(row) != len(self.headers):
                raise RuntimeError('Fila CSV inválida o cambio de cabecera; carga detenida.')
            try:
                values = tuple(float(row[i].strip().replace(',', '.')) for i in self.columns)
            except ValueError as exc:
                raise RuntimeError('El CSV no contiene temperaturas numéricas en las columnas seleccionadas.') from exc
            return check_temperatures(values)
        raise RuntimeError('El CSV todavía no contiene una muestra completa.')
