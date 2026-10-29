"""GPU Watts: Windows/NVIDIA, Python 3.10+, standard library only."""
import ctypes as C
import csv
import math
import ntpath
import os
from pathlib import Path
import queue
import sys
import threading
import time
from sensors import NativeSensors, CsvSensors, check_temperatures
from cutover import Cutover, MODES, read_cpu_times, AdaptiveHandoff, ADAPTIVE_MODE
from games import GameStore, steam_games, game_executables, game_name
from simulation import SimulatedGpu, PROFILES as SIM_PROFILES

P = C.c_void_p
U = C.c_uint
I = C.c_int


def bind(lib, name, args):
    f = getattr(lib, name)
    f.argtypes = args
    f.restype = I
    def call(*values):
        code = f(*values)
        if code:
            raise RuntimeError(f'{name}: error {code}')
    return call


class ProcessEntry(C.Structure):
    _fields_ = [('dwSize', U), ('cntUsage', U), ('th32ProcessID', U),
                ('th32DefaultHeapID', C.c_size_t), ('th32ModuleID', U),
                ('cntThreads', U), ('th32ParentProcessID', U),
                ('pcPriClassBase', C.c_long), ('dwFlags', U),
                ('szExeFile', C.c_wchar * 260)]


def process_entries():
    lib = C.WinDLL('kernel32', use_last_error=True)
    snapshot = lib.CreateToolhelp32Snapshot
    snapshot.argtypes, snapshot.restype = [U, U], P
    first, nxt = lib.Process32FirstW, lib.Process32NextW
    for fn in (first, nxt):
        fn.argtypes, fn.restype = [P, C.POINTER(ProcessEntry)], I
    close = lib.CloseHandle
    close.argtypes, close.restype = [P], I
    handle = snapshot(2, 0)
    if handle == P(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        entry = ProcessEntry()
        entry.dwSize = C.sizeof(entry)
        ok = first(handle, C.byref(entry))
        result = []
        while ok:
            result.append((entry.szExeFile, entry.th32ProcessID))
            ok = nxt(handle, C.byref(entry))
        error = C.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES
            raise C.WinError(error)
        return result
    finally:
        close(handle)


def process_names():
    return {name for name, _ in process_entries()}


def process_cpu_times(process):
    return read_cpu_times([pid for name,pid in process_entries() if name.casefold() == process])


def normalized_process(value):
    """Executable name to watch. Processes are matched by name, so a full path
    (also pasted with the quotes of «Copiar como ruta de acceso») is reduced to
    its file name."""
    value = value.strip().strip('"').strip()
    if not value:
        return ''
    if any(c in value for c in ('*', '?', '"')):
        raise ValueError('Escribe el nombre del .exe o su ruta completa, sin comodines.')
    if any(c in value for c in ('/', '\\', ':')):
        value = ntpath.basename(value)
        if not value.lower().endswith('.exe'):
            raise ValueError('La ruta tiene que terminar en el .exe del juego.')
    return (value if value.lower().endswith('.exe') else value + '.exe').casefold()


class Telemetry:
    def __init__(self):
        if sys.platform != 'win32' or C.sizeof(P) != 8:
            raise RuntimeError('Requiere Windows y Python de 64 bits.')
        paths = [str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'nvml.dll'),
                 str(Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'NVIDIA Corporation' / 'NVSMI' / 'nvml.dll')]
        for path in paths:
            try:
                self.lib = C.WinDLL(path)
                break
            except OSError:
                pass
        else:
            raise RuntimeError('No se encuentra el controlador NVIDIA (nvml.dll): este equipo no tiene tarjeta NVIDIA o le falta su controlador oficial.')
        bind(self.lib, 'nvmlInit_v2', [])()
        self.shutdown = bind(self.lib, 'nvmlShutdown', [])
        self.handle = bind(self.lib, 'nvmlDeviceGetHandleByIndex_v2', [U, C.POINTER(P)])
        self.name = bind(self.lib, 'nvmlDeviceGetName', [P, C.c_char_p, U])
        self.uuid = bind(self.lib, 'nvmlDeviceGetUUID', [P, C.c_char_p, U])
        self.power = bind(self.lib, 'nvmlDeviceGetPowerUsage', [P, C.POINTER(U)])
        self.temp = bind(self.lib, 'nvmlDeviceGetTemperature', [P, U, C.POINTER(U)])
        self.limit = bind(self.lib, 'nvmlDeviceGetEnforcedPowerLimit', [P, C.POINTER(U)])
        n = U()
        bind(self.lib, 'nvmlDeviceGetCount_v2', [C.POINTER(U)])(C.byref(n))
        self.devices = []
        for index in range(n.value):
            h, name, uuid = P(), C.create_string_buffer(128), C.create_string_buffer(96)
            self.handle(index, C.byref(h))
            self.name(h, name, 128)
            self.uuid(h, uuid, 96)
            self.devices.append((h, name.value.decode(), uuid.value.decode()))
        if not self.devices:
            raise RuntimeError('No hay GPU NVIDIA disponible.')

    def read(self, h):
        w, t, limit = U(), U(), U()
        self.power(h, C.byref(w))
        self.temp(h, 0, C.byref(t))
        self.limit(h, C.byref(limit))
        return w.value / 1000, t.value, limit.value / 1000


# Eight independent FMA chains, all written to device memory, prevent dead-code removal.
PTX = '''.version 6.0
.target sm_52
.address_size 64
.visible .entry heat(.param .u64 output, .param .u32 loops) {
.reg .pred p;
.reg .b32 r<6>;
.reg .b64 addr, offset, base;
.reg .f32 f<9>;
ld.param.u64 base, [output];
ld.param.u32 r4, [loops];
mov.u32 r0, %ctaid.x;
mov.u32 r1, %ntid.x;
mov.u32 r2, %tid.x;
mad.lo.u32 r3, r0, r1, r2;
cv t PLACEHOLDER
mov.u32 r5, 0;
INITIAL
LOOP:
MATH
add.u32 r5, r5, 1;
setp.lt.u32 p, r5, r4;
@p bra LOOP;
mul.wide.u32 offset, r3, 32;
add.u64 addr, base, offset;
STORE
ret;
}'''.replace('cv t PLACEHOLDER', 'cvt.rn.f32.u32 f8, r3;')
PTX = PTX.replace('INITIAL', '\n'.join(f'add.f32 f{i}, f8, {float(i+1):.1f};' for i in range(8)))
PTX = PTX.replace('MATH', '\n'.join(f'fma.rn.f32 f{i}, f{i}, 0f3F7FFF58, 0f38D1B717;' for _ in range(8) for i in range(8)))
PTX = PTX.replace('STORE', '\n'.join(f'st.global.f32 [addr+{4*i}], f{i};' for i in range(8))).encode() + b'\0'


# Grid-stride loop: a few blocks cover the whole fragment, so the number of
# blocks (busy SMs) sets the intensity instead of the size of the fragment.
MEMORY_PTX = b""".version 6.0
.target sm_52
.address_size 64
.visible .entry stream_memory(.param .u64 data, .param .u32 count) {
.reg .pred p;
.reg .b32 r<7>;
.reg .b64 base, offset, addr;
ld.param.u64 base, [data];
ld.param.u32 r4, [count];
mov.u32 r0, %ctaid.x;
mov.u32 r1, %ntid.x;
mov.u32 r2, %tid.x;
mad.lo.u32 r3, r0, r1, r2;
mov.u32 r6, %nctaid.x;
mul.lo.u32 r6, r6, r1;
LOOP:
setp.ge.u32 p, r3, r4;
@p bra END;
mul.wide.u32 offset, r3, 4;
add.u64 addr, base, offset;
ld.global.cg.u32 r5, [addr];
xor.b32 r5, r5, 0x9e3779b9;
st.global.wb.u32 [addr], r5;
add.u32 r3, r3, r6;
bra LOOP;
END:
ret;
}\0"""


# 1024-thread blocks: two cannot share an SM (1536 resident threads on RTX 30/40/50),
# so each block occupies its own SM and the block count is the number of busy SMs.
THREADS = 1024


class Compute:
    def __init__(self, uuid, mode='Core', vram_mb=512):
        self.lib = C.WinDLL('nvcuda.dll')
        b = lambda name, args: bind(self.lib, name, args)
        b('cuInit', [U])(0)
        count = I()
        b('cuDeviceGetCount', [C.POINTER(I)])(C.byref(count))
        get_uuid = b('cuDeviceGetUuid', [P, I])
        self.device = None
        for dev in range(count.value):
            raw = (C.c_ubyte * 16)()
            get_uuid(raw, dev)
            s = bytes(raw).hex()
            text = 'GPU-' + '-'.join([s[:8], s[8:12], s[12:16], s[16:20], s[20:]])
            if text.lower() == uuid.lower():
                self.device = dev
                break
        if self.device is None:
            raise RuntimeError('La GPU seleccionada no coincide con un dispositivo CUDA.')
        self.ctx, self.module, self.function = P(), P(), P()
        self.memory = C.c_uint64()
        self.vram = C.c_uint64()
        self.mem_module, self.mem_function = P(), P()
        self.mode = mode
        self.cursor = 0
        self.vram_bytes = int(vram_mb) * 1024 * 1024
        # Two alternating sets of (start, core end, done) events: one unit runs
        # while the next is queued. pending = (events, loops) of the queued unit.
        self.slots, self.turn, self.pending = [], 0, None
        self.launched = False
        self.retained = False
        self.free = b('cuMemFree_v2', [C.c_uint64])
        self.unload = b('cuModuleUnload', [P])
        self.release = b('cuDevicePrimaryCtxRelease_v2', [I])
        self.destroy_event = b('cuEventDestroy_v2', [P])
        try:
            b('cuDevicePrimaryCtxRetain', [C.POINTER(P), I])(C.byref(self.ctx), self.device)
            self.retained = True
            b('cuCtxSetCurrent', [P])(self.ctx)
            sm = I()
            b('cuDeviceGetAttribute', [C.POINTER(I), I, I])(C.byref(sm), 16, self.device)
            self.sms = sm.value
            b('cuModuleLoadData', [C.POINTER(P), P])(C.byref(self.module), C.cast(C.c_char_p(PTX), P))
            b('cuModuleGetFunction', [C.POINTER(P), P, C.c_char_p])(C.byref(self.function), self.module, b'heat')
            b('cuMemAlloc_v2', [C.POINTER(C.c_uint64), C.c_size_t])(C.byref(self.memory), self.sms * THREADS * 32)
            self.launch = b('cuLaunchKernel', [P, U, U, U, U, U, U, U, P, C.POINTER(P), C.POINTER(P)])
            self.sync = b('cuCtxSynchronize', [])
            self.wait_event = b('cuEventSynchronize', [P])
            self.record_event = b('cuEventRecord', [P, P])
            self.elapsed_event = b('cuEventElapsedTime', [C.POINTER(C.c_float), P, P])
            create_event = b('cuEventCreate', [C.POINTER(P), U])
            for _ in range(2):
                slot = (P(), P(), P())
                self.slots.append(slot)
                for event in slot:
                    # CU_EVENT_BLOCKING_SYNC: the wait sleeps instead of spinning a CPU core.
                    create_event(C.byref(event), 1)
            self.loops = 64
            if mode in ('VRAM', 'Mixto'):
                free, total = C.c_size_t(), C.c_size_t()
                b('cuMemGetInfo_v2', [C.POINTER(C.c_size_t), C.POINTER(C.c_size_t)])(C.byref(free), C.byref(total))
                if self.vram_bytes > min(free.value * .70, free.value - 1024**3):
                    raise RuntimeError('VRAM insuficiente: reduce los MB. Se reserva al menos 1 GiB libre.')
                b('cuMemAlloc_v2', [C.POINTER(C.c_uint64), C.c_size_t])(C.byref(self.vram), self.vram_bytes)
                b('cuModuleLoadData', [C.POINTER(P), P])(C.byref(self.mem_module), C.cast(C.c_char_p(MEMORY_PTX), P))
                b('cuModuleGetFunction', [C.POINTER(P), P, C.c_char_p])(C.byref(self.mem_function), self.mem_module, b'stream_memory')
        except Exception:
            self.close()
            raise

    def batch(self, intensity):
        """Queue one unit on `intensity` (0–1) of the SMs, then wait for the previous unit.

        The next unit is always queued before waiting, so the GPU never idles
        between kernels: the load is continuous and its size is how many SMs work,
        not full-GPU bursts separated by pauses."""
        active = max(1, min(self.sms, round(intensity*self.sms)))
        slot = self.slots[self.turn]
        self.turn ^= 1
        start, core_end, done = slot
        self.launched = True
        self.record_event(start, None)
        if self.mode in ('Core', 'Mixto'):
            loops = U(self.loops)
            args = (P * 2)(C.cast(C.byref(self.memory), P), C.cast(C.byref(loops), P))
            self.launch(self.function, active, 1, 1, THREADS, 1, 1, 0, None, args, None)
        self.record_event(core_end, None)
        if self.mode in ('VRAM', 'Mixto'):
            self.memory_passes(active)
        self.record_event(done, None)
        previous, self.pending = self.pending, (slot, self.loops)
        if previous is not None:
            self.finish(*previous)

    def drain(self):
        """Wait for the queued unit, leaving the GPU without our work. Returns its GPU seconds."""
        if self.pending is None:
            return 0.0
        pending, self.pending = self.pending, None
        return self.finish(*pending)

    def finish(self, slot, loops):
        start, core_end, done = slot
        started = time.monotonic()
        self.wait_event(done)
        if time.monotonic()-started > .25:
            raise RuntimeError('Respuesta GPU demasiado lenta (>250 ms). Prueba detenida.')
        if self.mode in ('Core', 'Mixto'):
            # Calibrate using GPU event time, not Python/driver wall time, from the
            # loops this unit was launched with (the next one is already queued).
            core = self.elapsed(start, core_end)
            self.loops = max(1, min(65536, int(loops * min(2.0, max(0.5, 0.006/core)))))
        return self.elapsed(start, done)

    def elapsed(self, first, last):
        milliseconds = C.c_float()
        self.elapsed_event(C.byref(milliseconds), first, last)
        return max(milliseconds.value/1000, .000001)

    def memory_passes(self, active):
        # Fewer SMs move less data per pass: scale the passes so a unit keeps a
        # similar length. All addresses stay inside our own allocation.
        for _ in range(max(1, round(16*active/self.sms))):
            size = min(16*1024*1024, self.vram_bytes-self.cursor)
            pointer, count = C.c_uint64(self.vram.value+self.cursor), U(size//4)
            args = (P * 2)(C.cast(C.byref(pointer), P), C.cast(C.byref(count), P))
            self.launch(self.mem_function, active, 1, 1, THREADS, 1, 1, 0, None, args, None)
            self.cursor = (self.cursor+size) % self.vram_bytes

    def close(self):
        try:
            if self.launched:
                self.pending, self.launched = None, False
                self.sync()  # never free memory that a queued kernel still uses
            for slot in self.slots:
                for event in slot:
                    if event.value:
                        self.destroy_event(event)
            if self.vram.value:
                self.free(self.vram.value)
            if self.mem_module.value:
                self.unload(self.mem_module)
            if self.memory.value:
                self.free(self.memory.value)
        finally:
            try:
                if self.module.value:
                    self.unload(self.module)
            finally:
                if self.retained:
                    self.release(self.device)
                    self.retained = False


def next_duty(duty, target, measured, limit):
    error = target - measured
    if abs(error) <= max(3, target * 0.03):
        return duty
    step = max(-0.20, min(0.08, error / max(limit, 1) * 0.60))
    return max(0.0, min(1.0, duty + step))


def validate(target, duration, cutoff):
    if not all(math.isfinite(x) for x in (target, duration, cutoff)):
        raise ValueError('Introduce números finitos.')
    if not 10 <= target <= 1000 or not 1 <= duration <= 86400 or not 45 <= cutoff <= 85:
        raise ValueError('Rangos: 10–1000 W; 1–86400 segundos; 45–85 °C.')


def fade_duty(initial, elapsed, seconds):
    if seconds <= 0:
        return 0.0
    t = max(0.0, min(1.0, elapsed/seconds))
    return initial * (1.0 - 3.0*t*t + 2.0*t*t*t)


def load_shape(duty, level=None):
    """Split average activity into (intensity, share of time).

    Alone on the GPU we work continuously and duty is the fraction of SMs.
    With the watched game open, Windows time-slices both processes: we keep
    the intensity we had when it appeared (`level`, comparable to the game's)
    and shrink our turn instead, so the game fills our gaps. Continuous work
    at low intensity would take half the GPU time at low power."""
    if level:
        return level, max(0.0, min(1.0, duty/level))
    return duty, 1.0


class Pacer:
    """Runs our units and, when sharing the GPU with the game, the gaps between them.

    Windows waits are coarse (~15 ms with the default timer): asking for a
    0.4 ms gap after each 6 ms unit would really wait ~15 ms and turn a 94 %
    turn into ~30 %. So the gap owed after each unit is added up, taken once
    it is worth a wait, and the time really waited is what gets paid off."""
    MIN_WAIT = .015

    def __init__(self):
        self.owed = 0.0

    def unit(self, compute, duty, level, stop, until):
        """One step: a unit of work, or part of the gap owed (waits are capped at
        0.1 s so the caller keeps sampling, and no unit runs while a gap is owed)."""
        intensity, share = load_shape(duty, level)
        if share >= 1:
            self.owed = 0.0
            compute.batch(intensity)
            return
        if self.owed >= self.MIN_WAIT:
            started = time.monotonic()
            stop.wait(min(self.owed, .1, max(0, until-started)))
            self.owed -= time.monotonic()-started
            return
        compute.batch(intensity)
        self.owed = min(1.0, self.owed + compute.drain()*(1/max(share, .01)-1))


def ramp_down(compute, initial_duty, seconds, telemetry, handle, sensors, cutoffs, stop, messages, report=None, level=None):
    started = time.monotonic()
    sample_at = started
    pacer = Pacer()
    while not stop.is_set():
        now = time.monotonic()
        if now-started >= seconds:
            break
        duty = fade_duty(initial_duty, now-started, seconds)
        if now >= sample_at:
            try:
                # During withdrawal, thresholds have already requested a stop.
                # Keep displaying readings while activity decreases to zero.
                temperatures = check_temperatures(sensors.read(), require_memory=getattr(sensors,'monitor_vram',True))
                measured, temp, limit = telemetry.read(handle)
                messages.put(('temperatures', temperatures))
                messages.put(('sample',(measured,temp,duty,max(0,seconds-(now-started)),'Retirando precarga gradualmente')))
                if report:
                    report(now, measured, temperatures, duty, limit)
            except Exception as exc:
                messages.put(('status', f'Retirada gradual; lectura/registro no disponible: {exc}'))
            sample_at = now+.25
        # Duty regulates average activity, not the GPU's instantaneous electrical power.
        if duty <= .0001:
            stop.wait(min(.02, max(0,seconds-(time.monotonic()-started))))
            continue
        pacer.unit(compute, duty, level, stop, started+seconds)


def run_load(telemetry, device, target, duration, cutoff, stop, messages, process='', close_on_process=True, sensors=None, mem_cutoff=85, mode='Core', vram_mb=512, trigger=None, ramp_seconds=0, make_compute=None, processes=None, watch_seconds=0):
    # make_compute/processes replace CUDA and the Windows process list in simulation.
    processes = processes or process_names
    compute = None
    reason = 'Tiempo completado'
    detected = False
    samples = []
    output = writer = None
    duty = .01
    level = None  # intensity kept while the watched process is open (see load_shape)
    start = time.monotonic()
    try:
        if not math.isfinite(ramp_seconds) or not 0 <= ramp_seconds <= 30:
            raise ValueError('La retirada gradual debe durar entre 0 y 30 segundos.')
        trigger = trigger or Cutover(mode='Al aparecer')
        adaptive = AdaptiveHandoff(target,ramp_seconds,trigger.hold,trigger.grace,watch_seconds) if trigger.mode == ADAPTIVE_MODE else None
        already_open = bool(process) and process in {n.casefold() for n in processes()}
        if already_open and trigger.mode == 'Al aparecer':
            detected = True
            reason = f'Proceso ya abierto: {process}; no se ha iniciado carga'
            return
        if already_open and adaptive is not None:
            # The jump from idle has already happened: preloading now only takes GPU time from the game.
            reason = (f'El juego ya está abierto ({process}): la precarga solo sirve antes de abrirlo. '
                      'Ciérralo, pulsa INICIAR y ábrelo cuando ponga «Base aproximada alcanzada».')
            return
        h, name, uuid = telemetry.devices[device]
        sensors = sensors or NativeSensors(telemetry, h)
        cutoffs = (cutoff, mem_cutoff)
        temperatures = check_temperatures(sensors.read(), cutoffs, require_memory=getattr(sensors, 'monitor_vram', True))
        messages.put(('temperatures', temperatures))
        idle, temp, limit = telemetry.read(h)
        # Rough intensity that holds the base, for a game that appears before the base is reached.
        base_intensity = max(.05, min(1.0, (target-idle)/max(limit-idle, 1)))
        if process and trigger.uses_gpu:
            if trigger.watts <= target + max(10, target*.10):
                raise ValueError('El umbral GPU debe superar el objetivo de precarga al menos un 10% o 10 W.')
            if trigger.watts > limit:
                raise ValueError('El umbral de relevo GPU supera el límite de potencia vigente.')
        if target > limit:
            raise ValueError(f'Objetivo superior al límite vigente ({limit:.0f} W).')
        if temp >= cutoff:
            raise ValueError('Temperatura inicial igual o superior a la parada.')
        if idle > target + 5:
            raise ValueError(f'Consumo inicial {idle:.0f} W: cierra otras cargas o elige un objetivo mayor.')
        if stop.is_set():
            return
        messages.put(('status', 'Preparando CUDA; la duración comienza con la carga…'))
        compute = (make_compute or Compute)(uuid, mode, vram_mb)
        folder = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'GPU_Watts' / 'logs'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{time.time_ns()}.csv'
        messages.put(('log', str(path)))
        output = path.open('w', newline='', encoding='utf-8')
        writer = csv.writer(output)
        writer.writerow(['seconds', 'target_w', 'board_average_w', 'core_c', 'vram_c', 'duty', 'limit_w', 'mode', 'allocated_vram_mb', 'vram_temperature_monitored', 'watched_process_present', 'process_cpu_percent', 'cutover_condition', 'phase'])
        start = time.monotonic()
        deadline = start + duration
        sample_at = start
        pacer = Pacer()
        base_reached, shown_phase, watch_extended = False, None, False
        # Watts we add per unit of duty, learnt before the game while every watt
        # above idle is ours. The driver only reports the whole board, so with the
        # game open our part is this times the duty of the last half second.
        watts_per_duty, last_duty, splitting = limit-idle, duty, False
        duty = 0.01
        measured = idle
        while not stop.is_set() and time.monotonic() < deadline:
            now = time.monotonic()
            if now >= sample_at:
                temperatures = check_temperatures(sensors.read(), cutoffs, require_memory=getattr(sensors, 'monitor_vram', True))
                messages.put(('temperatures', temperatures))
                measured, temp, limit = telemetry.read(h)
                process_present = bool(process) and process in {n.casefold() for n in processes()}
                if adaptive is not None and adaptive.played and not process_present:
                    # The player closed the game while we were watching: nothing left to cushion.
                    detected = True
                    reason = f'Relevo: {process} · el juego se ha cerrado durante la vigilancia'
                    break
                if adaptive is not None and watch_seconds and process_present and not watch_extended:
                    # The time limit must not cut the watch short.
                    deadline = max(deadline, now + watch_seconds + 30)
                    watch_extended = True
                if not process_present:
                    level = None
                elif level is None:
                    # Game opened before the base: a weak continuous load would take half
                    # the GPU time at low power, keep the total under the base and never
                    # hand off. Work at base intensity in short turns instead.
                    level = duty if duty >= base_intensity/2 else base_intensity
                counters = process_cpu_times(process) if process_present and trigger.uses_cpu else None
                # Our part of the board reading (estimated; see watts_per_duty) and the rest, the game's.
                if not process_present and last_duty >= .05:
                    watts_per_duty = max(0.0, measured-idle)/last_duty
                own = max(0.0, min(measured, watts_per_duty*last_duty))
                ready = False
                if process:
                    if adaptive is not None:
                        duty, ready, description = adaptive.update(time.monotonic(),process_present,measured,duty,limit,measured-own)
                        trigger.present = process_present
                        trigger.description = description
                    else:
                        ready = trigger.update(time.monotonic(),process_present,measured,counters)
                if ready:
                    detected = True
                    reason = f'Relevo: {process} · {trigger.description}'
                    samples.append(measured)
                    writer.writerow([round(time.monotonic()-start,3), target, measured, *temperatures, round(duty,4), limit, mode, vram_mb if mode != 'Core' else 0, getattr(sensors,'monitor_vram',True), process_present, trigger.cpu_percent, trigger.mode, 'relevo'])
                    output.flush()
                    messages.put(('watch', trigger.description))
                    messages.put(('split', (own, measured-own)))
                    break
                if process:
                    messages.put(('watch', trigger.description))
                if temp >= cutoff:
                    reason = f'Parada por temperatura: {temp} °C'
                    break
                if target > limit:
                    reason = 'El límite de potencia del controlador ha bajado'
                    break
                # A software guard based on averaged readings, not a hardware wattage cap.
                if not process_present and measured > max(target + 30, target * 1.35):
                    reason = f'Parada por consumo medido elevado: {measured:.0f} W'
                    break
                samples.append(measured)
                # Adaptive mode regulates support inside AdaptiveHandoff above.
                # Legacy threshold modes freeze it while waiting for their trigger.
                if not process_present:
                    duty = next_duty(duty, target, measured, limit)
                elapsed = now - start
                writer.writerow([round(elapsed, 3), target, measured, *temperatures, round(duty, 4), limit, mode, vram_mb if mode != 'Core' else 0, getattr(sensors, 'monitor_vram', True), process_present, trigger.cpu_percent, trigger.mode if process else 'desactivado', 'carga'])
                output.flush()
                state = 'Objetivo fuera de alcance con esta carga' if duty >= 0.999 and measured < target - 10 else 'Ajustando carga'
                if adaptive is not None:
                    if process_present:
                        state = 'Relevo adaptativo: ajustando nuestra aportación'
                    elif abs(measured-target) <= max(10,target*.05):
                        state = 'Base aproximada alcanzada; puedes iniciar el juego'
                # What the player should do now. Once reached, the base stays "reached"
                # so the notice does not flicker with the noise of the readings.
                base_reached = base_reached or abs(measured-target) <= max(10, target*.05)
                phase = 'relevo' if process_present else 'base' if base_reached else 'subiendo'
                if process_present and adaptive is not None and adaptive.watching(time.monotonic()):
                    phase = 'vigilando' if adaptive.committed else 'sosteniendo'
                if phase != shown_phase:
                    messages.put(('phase', phase))
                    shown_phase = phase
                if process_present:
                    messages.put(('split', (own, measured-own)))
                    splitting = True
                elif splitting:
                    messages.put(('split', None))
                    splitting = False
                last_duty = duty
                messages.put(('sample', (measured, temp, duty, max(0, deadline-now), state)))
                sample_at = time.monotonic() + 0.5
            if duty <= 0:
                stop.wait(min(0.02, max(0, deadline-time.monotonic())))
            else:
                pacer.unit(compute, duty, level, stop, deadline)
        if stop.is_set():
            reason = 'Detenido por el usuario' 
    except Exception as exc:
        reason = f'Error: {exc}'
    finally:
        if compute is not None:
            if ramp_seconds > 0 and duty > 0:
                try:
                    messages.put(('status', f'{reason} · iniciando retirada de {ramp_seconds:g} s'))
                    def report_ramp(now, measured, temperatures, duty_now, limit_now):
                        samples.append(measured)
                        if writer is not None and output is not None and not output.closed:
                            writer.writerow([round(now-start,3),target,measured,*temperatures,round(duty_now,4),limit_now,mode,vram_mb if mode != 'Core' else 0,getattr(sensors,'monitor_vram',True),trigger.present,trigger.cpu_percent,trigger.mode if process else 'desactivado','retirada gradual'])
                            output.flush()
                    # Ordinary stop/X/Esc requests cannot interrupt withdrawal.
                    # Force-close terminates the OS process instead.
                    ramp_down(compute,duty,ramp_seconds,telemetry,h,sensors,cutoffs,threading.Event(),messages,report_ramp,level)
                except Exception as exc:
                    reason += f' | No se pudo completar la retirada CUDA: {exc}'
            try:
                compute.close()
            except Exception as exc:
                reason += f' | Liberación CUDA: {exc}'
        if output is not None:
            try:
                output.close()
            except Exception as exc:
                reason += f' | Registro: {exc}'
        if samples:
            messages.put(('summary', f'Máximo medido: {max(samples):.0f} W · última lectura: {samples[-1]:.0f} W · objetivo {target:.0f} W'))
        messages.put(('done', reason))
        if detected and close_on_process:
            messages.put(('autoclose', True))


HELP = {
    'game': 'Tus juegos guardados, el más usado primero. Al elegir uno se rellenan su ejecutable y los ajustes '
            'con los que lo usaste la última vez. Cada INICIAR guarda los ajustes del juego.',
    'steam': 'Añade un juego de tu biblioteca de Steam sin tener que abrirlo: eliges el juego y su .exe.',
    'forget': 'Quita de la lista el juego elegido, con sus ajustes.',
    'browse': 'Para juegos que no son de Steam (Epic, GOG, 3DMark suelto…): eliges su .exe en el disco y se '
              'guarda en la lista con el nombre de su carpeta.',
    'process': 'El .exe del juego que GPU Watts espera ver aparecer para hacer el relevo. Se rellena al elegir un '
               'juego de la lista; también puedes escribirlo, pegar su ruta completa (se queda solo con el '
               'nombre) o elegirlo en el desplegable con el juego abierto. '
               'Aunque lo abras desde Steam, Steam arranca este mismo .exe. Vacío = sin relevo: solo carga '
               'durante el tiempo máximo.',
    'base': 'Consumo TOTAL de la tarjeta que se mantiene antes de abrir el juego, en W. No son W de más: incluye '
            'el escritorio y todo lo demás. Es una media aproximada (la lectura promedia ~1 s), no un techo '
            'instantáneo.',
    'smooth': 'Lo despacio que GPU Watts quita (y repone) su carga. Con 5 s, retirar toda la carga lleva al menos '
              '5 s. Más alto = más suave, pero el relevo dura más. También se usa al pulsar PARAR.',
    'duration': 'Si el relevo no ha terminado en este tiempo, contado desde INICIAR, GPU Watts retira su carga '
                'igualmente y para. Súbelo si tardas en abrir el juego.',
    'mode': 'Core: cálculo puro (lo normal). VRAM: lee y escribe memoria de vídeo. Mixto: alterna los dos.',
    'vram_mb': 'Solo para VRAM y Mixto: tamaño del bloque de memoria que se recorre, 64–4096 MiB. Siempre deja al '
               'menos 1 GiB libre. Es carga, no una prueba de la memoria.',
    'trigger': 'Relevo adaptativo (recomendado): mantiene la base hasta que aparece el juego; después va quitando '
               'su carga a medida que el juego consume, y vuelve a ayudar si el consumo cae (pantallas de carga). '
               'Termina cuando su carga es cero y la tarjeta se sostiene sola al 95 % de la base. Si el juego gasta '
               'menos que la base, en cuanto su parte llega al 70 % de la base ya está jugando: GPU Watts se retira '
               'del todo, no vuelve a ayudar y termina igualmente.\n\nModos clásicos: congelan la '
               'carga al aparecer el juego y esperan a que se cumpla un umbral de W o de CPU.',
    'grace': 'Segundos desde que aparece el juego antes de poder dar el relevo por terminado. Evita cerrar en el '
             'primer instante.',
    'hold': 'Tiempo seguido que debe cumplirse la condición para dar el relevo por bueno. En el adaptativo: sin '
            'carga propia y la tarjeta al 95 % de la base. Si falla, la cuenta empieza de nuevo.',
    'gpu_w': 'Solo modos clásicos: consumo TOTAL de la tarjeta a partir del cual se da el relevo. Tiene que '
             'superar la base al menos un 10 % o 10 W.',
    'cpu': 'Solo modos clásicos: uso de CPU del juego, en % del equipo entero, a partir del cual se da el relevo. '
           'Con 16 hilos, un hilo al 100 % es ~6 %.',
    'close': 'Al terminar el relevo, GPU Watts se cierra solo. Si no se cierra, el estado dice por qué paró.',
    'watch': 'Para juegos con menú → pantalla de carga → partida. 0 = como siempre: GPU Watts termina en cuanto el '
             'juego coge el consumo. Con más de 0 se queda vigilando ese tiempo desde que se abre el juego, sin '
             'cargar nada mientras el juego consume. Si el consumo cae mucho (pantalla de carga: el juego por debajo '
             'de la mitad de la base), vuelve a sostener la base para que el salto al entrar con el personaje parta '
             'de ahí; cuando el juego vuelve a consumir, se retira otra vez. Al acabar el tiempo se retira y termina; '
             'si cierras el juego antes, también. El tiempo máximo se amplía solo si hace falta. Ejemplo: 180.',
    'gpu': 'La tarjeta NVIDIA que se carga y se vigila.',
    'source': 'De dónde salen las temperaturas. NVIDIA directo: el controlador da core y VRAM. CSV HWiNFO: si el '
              'controlador no da la VRAM, HWiNFO puede escribir un CSV (registro cada 0,5–1 s) y se leen de ahí; '
              'elige el CSV y sus dos columnas. Solo core: la VRAM no se vigila.\n\nEn las GeForce el controlador '
              'normalmente no da la VRAM, así que «NVIDIA directo» falla: usa «solo core» o el CSV de HWiNFO. '
              'Lo que elijas se recuerda para la próxima vez.',
    'core_c': 'Si el core de la GPU llega a esta temperatura, GPU Watts empieza a retirar su carga. Es un valor '
              'prudente de la aplicación, no un límite oficial.',
    'vram_c': 'Si la memoria de la GPU llega a esta temperatura, GPU Watts empieza a retirar su carga. Es un valor '
              'prudente de la aplicación, no un límite oficial.',
    'start': 'Empieza a subir la carga hacia la base. Espera al aviso verde «ABRE EL JUEGO AHORA» (con un pitido) '
             'y entonces abre el juego.',
    'stop': 'Retira la carga poco a poco (según la Suavidad) y para. También con Esc o cerrando la ventana.',
    'force': 'Cierra al instante, sin retirada gradual. Solo si algo va mal.',
    'split': 'ESTIMACIÓN. El controlador NVIDIA solo da el consumo de la tarjeta entera, no el de cada programa. '
             'Antes de abrir el juego, todo lo que pasa del reposo es de GPU Watts: así se sabe cuántos W pone por '
             'cada % de aportación. Con el juego abierto, su parte se calcula con la aportación que le queda, y el '
             'resto es del juego (incluido el escritorio). Cuando GPU Watts llega a 0 %, todo es del juego.',
    'sim_watts': 'SOLO SIMULACIÓN. Cuánto gastaría el juego de mentira cuando ya está jugando (tras 6 s de pantalla '
                 'de carga). Con la tarjeta real no existe: el juego gasta lo que gasta.\n\nNo es la «Base de '
                 'consumo», que es lo que GPU Watts mantiene antes del juego. Prueba un juego por encima de la base '
                 '(300) y otro por debajo (150) para ver la diferencia: por debajo, GPU Watts no llega a retirarse.',
    'sim_open': 'SOLO SIMULACIÓN. Hace aparecer el «Ejecutable vigilado» del paso 1 como si hubieras abierto el juego. '
                'Úsalo después de INICIAR, cuando arriba salga el aviso verde «ABRE EL JUEGO AHORA».',
    'sim_close': 'SOLO SIMULACIÓN. Hace desaparecer el juego de mentira, como si lo cerraras.',
    'sim_profile': 'SOLO SIMULACIÓN. «Carga y juego»: 6 s de pantalla de carga y luego juega. «Menú, carga y juego»: '
                   '6 s de carga, 12 s de menú que ya consume mucho, 8 s de otra pantalla de carga y luego el salto al '
                   'entrar con el personaje. Con el segundo se ve para qué sirve «Vigilar tras abrir el juego».',
}


def main(simulate=False):
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = tk.Tk()
    # Errors inside the window (button callbacks, timers) are logged and shown too.
    root.report_callback_exception = lambda kind, value, trace: report_error(kind, value, trace, parent=root)
    root.geometry('1080x780')
    root.minsize(920, 560)
    if not simulate:
        try:
            telemetry = Telemetry()
        except Exception as exc:
            root.withdraw()
            if not messagebox.askyesno('GPU Watts', f'{exc}\n\n¿Abrir en modo simulación para ver y probar el panel? No carga ninguna tarjeta.'):
                root.destroy()
                return
            root.deiconify()
            simulate = True
    if simulate:
        telemetry = SimulatedGpu()
    root.title('GPU Watts 1.0 · SIMULACIÓN (no carga ninguna tarjeta)' if simulate else 'GPU Watts 1.0 · carga temporal NVIDIA')
    style = ttk.Style(root)
    style.configure('Step.TLabelframe.Label', font=('Segoe UI', 11, 'bold'))
    style.configure('Big.TButton', font=('Segoe UI', 12, 'bold'), padding=(18, 8))
    style.configure('Hint.TLabel', foreground='#555555')
    style.configure('Sim.TLabelframe.Label', font=('Segoe UI', 11, 'bold'), foreground='#b00020')
    texts = dict(HELP)
    if simulate:
        texts['steam'] += ('\n\nEN SIMULACIÓN: muestra una biblioteca de ejemplo con nombres de .exe inventados, '
                           'para ensayar. Con la tarjeta real lee tu Steam de verdad.')
        texts['base'] += ('\n\nAjuste REAL del programa: también existe con la tarjeta de verdad. No lo confundas con '
                          '«W del juego simulado», que solo existe en la simulación.')
    canvas = tk.Canvas(root, highlightthickness=0)
    scrollbar = ttk.Scrollbar(root, orient='vertical', command=canvas.yview)
    scrollbar.pack(side='right', fill='y')
    canvas.pack(side='left', fill='both', expand=True)
    canvas.configure(yscrollcommand=scrollbar.set)
    box = ttk.Frame(canvas, padding=18)
    window_id = canvas.create_window((0, 0), window=box, anchor='nw')
    box.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window_id, width=e.width))
    root.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(-int(e.delta/120), 'units'))

    def tip(widget, text):
        """Explanation shown while the mouse rests on the widget."""
        state = {'window': None, 'job': None}
        def show():
            state['job'] = None
            window = tk.Toplevel(widget)
            window.wm_overrideredirect(True)
            window.attributes('-topmost', True)
            window.geometry(f'+{widget.winfo_rootx()+10}+{widget.winfo_rooty()+widget.winfo_height()+4}')
            tk.Label(window, text=text, justify='left', wraplength=420, background='#ffffe1', foreground='#000000',
                     relief='solid', borderwidth=1, padx=8, pady=6, font=('Segoe UI', 9)).pack()
            state['window'] = window
        def enter(_):
            if state['job'] is None and state['window'] is None:
                state['job'] = widget.after(350, show)
        def leave(_):
            if state['job'] is not None:
                widget.after_cancel(state['job'])
                state['job'] = None
            if state['window'] is not None:
                state['window'].destroy()
                state['window'] = None
        widget.bind('<Enter>', enter, add='+')
        widget.bind('<Leave>', leave, add='+')
        widget.bind('<ButtonPress>', leave, add='+')
    def step(parent, title):
        frame = ttk.LabelFrame(parent, text=title, padding=(12, 8), style='Step.TLabelframe')
        frame.pack(fill='x', pady=(0, 12))
        return frame
    def label(parent, text, key, row, column):
        widget = ttk.Label(parent, text=f'{text}  ⓘ')
        widget.grid(row=row, column=column, sticky='w', padx=(0 if column == 0 else 22, 8), pady=4)
        tip(widget, texts[key])
        return widget
    def field(parent, text, key, default, row, column=0, width=8):
        name = label(parent, text, key, row, column)
        entry = ttk.Entry(parent, width=width)
        entry.insert(0, default)
        entry.grid(row=row, column=column+1, sticky='w', pady=4)
        tip(entry, texts[key])
        return name, entry

    header = ttk.Frame(box)
    header.pack(fill='x')
    ttk.Label(header, text='GPU Watts', font=('Segoe UI', 22, 'bold')).pack(side='left')
    ttk.Label(header, text='Sube la tarjeta a una base antes del juego y se retira cuando el juego coge el consumo.',
              style='Hint.TLabel').pack(side='left', padx=14, pady=(10, 0))
    ttk.Label(box, text='Cómo se usa:   1. Elige el juego   →   2. Pulsa INICIAR   →   3. Con el aviso verde «ABRE EL JUEGO AHORA», ábrelo.',
              font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(4, 14))
    columns = ttk.Frame(box)
    columns.pack(fill='both', expand=True)
    left = ttk.Frame(columns)
    left.pack(side='left', fill='both', expand=True)
    right = ttk.Frame(columns, width=380)
    right.pack(side='left', fill='y', padx=(18, 0))

    # 1 · Juego
    store = GameStore()
    saved_order = []
    section = step(left, '1 · Juego')
    section.columnconfigure(1, weight=1)
    label(section, 'Juego guardado', 'game', 0, 0)
    game_row = ttk.Frame(section)
    game_row.grid(row=0, column=1, sticky='ew', pady=4)
    game_box = ttk.Combobox(game_row, state='readonly')
    game_box.pack(side='left', fill='x', expand=True)
    tip(game_box, texts['game'])
    steam_button = ttk.Button(game_row, text='Steam…')
    steam_button.pack(side='left', padx=(8, 0))
    tip(steam_button, texts['steam'])
    browse_button = ttk.Button(game_row, text='Buscar .exe…')
    browse_button.pack(side='left', padx=(6, 0))
    tip(browse_button, texts['browse'])
    forget_button = ttk.Button(game_row, text='Olvidar')
    forget_button.pack(side='left', padx=(6, 0))
    tip(forget_button, texts['forget'])
    label(section, 'Ejecutable vigilado', 'process', 1, 0)
    process_box = ttk.Combobox(section)
    process_box.grid(row=1, column=1, sticky='ew', pady=4)
    tip(process_box, texts['process'])
    def refresh_processes():
        try:
            process_box.configure(values=sorted(process_names(), key=str.casefold))
        except Exception as exc:
            messagebox.showerror('Procesos', str(exc))
    process_box.configure(postcommand=refresh_processes)

    # 2 · Precarga
    section = step(left, '2 · Precarga antes del juego')
    _, base_entry = field(section, 'Base de consumo (W)', 'base', '200', 0)
    _, smooth_entry = field(section, 'Suavidad (s, 0,5–30)', 'smooth', '5', 0, 2)
    _, duration_entry = field(section, 'Tiempo máximo (s)', 'duration', '120', 1)
    label(section, 'Tipo de carga', 'mode', 1, 2)
    mode_box = ttk.Combobox(section, state='readonly', values=['Core', 'VRAM', 'Mixto'], width=8)
    mode_box.set('Core')
    mode_box.grid(row=1, column=3, sticky='w', pady=4)
    tip(mode_box, texts['mode'])
    _, vram_box = field(section, 'VRAM a recorrer (MiB)', 'vram_mb', '512', 2, 2)
    def update_load_mode(_=None):
        if str(mode_box.cget('state')) != 'disabled':
            vram_box.configure(state='disabled' if mode_box.get() == 'Core' else 'normal')
    mode_box.bind('<<ComboboxSelected>>', update_load_mode)

    # 3 · Relevo
    section = step(left, '3 · Relevo al juego')
    section.columnconfigure(3, weight=1)
    label(section, 'Cómo decide el relevo', 'trigger', 0, 0)
    trigger_box = ttk.Combobox(section, state='readonly', values=list(MODES))
    trigger_box.set(ADAPTIVE_MODE)
    trigger_box.grid(row=0, column=1, columnspan=3, sticky='ew', pady=4)
    tip(trigger_box, texts['trigger'])
    _, grace_entry = field(section, 'Espera inicial (s)', 'grace', '2', 1)
    _, hold_entry = field(section, 'Confirmar durante (s)', 'hold', '2', 1, 2)
    classic = [*field(section, 'Umbral GPU (W)', 'gpu_w', '240', 2), *field(section, 'CPU del juego (%)', 'cpu', '10', 2, 2)]
    gpu_w_entry, cpu_entry = classic[1], classic[3]
    adaptive_only = [*field(section, 'Vigilar tras abrir el juego (s)', 'watch', '0', 3)]
    watch_entry = adaptive_only[1]
    close_on_process = tk.BooleanVar(value=True)
    auto_close_box = ttk.Checkbutton(section, text='Cerrar GPU Watts al terminar el relevo  ⓘ', variable=close_on_process)
    auto_close_box.grid(row=4, column=0, columnspan=4, sticky='w', pady=(6, 0))
    tip(auto_close_box, texts['close'])
    def update_trigger_mode(_=None):
        adaptive = trigger_box.get() == ADAPTIVE_MODE
        for widget in classic:
            widget.grid_remove() if adaptive else widget.grid()
        for widget in adaptive_only:
            widget.grid() if adaptive else widget.grid_remove()
    trigger_box.bind('<<ComboboxSelected>>', update_trigger_mode)

    # 4 · Tarjeta y temperaturas
    section = step(left, '4 · Tarjeta y temperaturas')
    section.columnconfigure(1, weight=1)
    label(section, 'Tarjeta', 'gpu', 0, 0)
    gpu = ttk.Combobox(section, state='readonly', values=[f'{i}: {v[1]}' for i, v in enumerate(telemetry.devices)])
    gpu.current(0)
    gpu.grid(row=0, column=1, columnspan=3, sticky='ew', pady=4)
    label(section, 'Fuente de temperatura', 'source', 1, 0)
    source_row = ttk.Frame(section)
    source_row.grid(row=1, column=1, columnspan=3, sticky='ew', pady=4)
    direct, csv_source, only_core_source = 'NVIDIA directo', 'CSV HWiNFO', 'NVIDIA solo core (sin vigilancia VRAM)'
    sources = [direct, csv_source, only_core_source]
    source_box = ttk.Combobox(source_row, state='readonly', values=sources, width=36)
    # A setting of this PC, not of a game: remembered between sessions.
    source_box.set(store.prefs.get('source') if store.prefs.get('source') in sources else sources[0])
    source_box.pack(side='left')
    tip(source_box, texts['source'])
    csv_path = tk.StringVar()
    column_boxes = []
    def choose_csv():
        from tkinter import filedialog
        path = filedialog.askopenfilename(filetypes=[('Registro CSV', '*.csv')])
        if not path:
            return
        try:
            _, _, headers = CsvSensors.inspect(path)
            labels = [f'{i}: {name}' for i, name in enumerate(headers)]
            for selector in column_boxes:
                selector.configure(values=labels)
                selector.set('')
            csv_path.set(path)
            source_box.set('CSV HWiNFO')
            choose_source()
            status.set('Selecciona core y VRAM de la GPU correcta, en °C.')
        except Exception as exc:
            messagebox.showerror('CSV', str(exc))
    csv_button = ttk.Button(source_row, text='Elegir CSV activo…', command=choose_csv)
    csv_widgets = []
    for row, text in ((2, 'Columna core del CSV'), (3, 'Columna VRAM del CSV')):
        name = ttk.Label(section, text=text)
        name.grid(row=row, column=0, sticky='w', pady=2)
        selector = ttk.Combobox(section, state='readonly')
        selector.grid(row=row, column=1, columnspan=3, sticky='ew', pady=2)
        column_boxes.append(selector)
        csv_widgets += [name, selector]
    core_name, core_entry = field(section, 'Retirar si core llega a (°C)', 'core_c', '75', 4)
    _, vram_c_entry = field(section, 'Retirar si VRAM llega a (°C)', 'vram_c', '85', 4, 2)
    vram_note = ttk.Label(section, style='Hint.TLabel', wraplength=520, text=(
        'Tu tarjeta no da la temperatura de la VRAM por el controlador (lo normal en las GeForce): se vigila solo '
        'el core. Para vigilar también la VRAM, usa «CSV HWiNFO».'))
    vram_note.grid(row=5, column=0, columnspan=4, sticky='w', pady=(4, 0))
    def driver_gives_vram():
        handle = telemetry.devices[gpu.current()][0]
        try:
            sensors = telemetry.sensors() if simulate else NativeSensors(telemetry, handle)
            return sensors.memory_temperature() is not None
        except Exception:
            return False
    def offer_sources(_=None):
        # Do not offer «NVIDIA directo» on a card where it can only fail.
        usable = sources if driver_gives_vram() else [only_core_source, csv_source]
        source_box.configure(values=usable)
        if source_box.get() not in usable:
            source_box.set(usable[0])
        if direct in usable:
            vram_note.grid_remove()
        else:
            vram_note.grid()
        update_sensor_mode()
    gpu.bind('<<ComboboxSelected>>', offer_sources)
    # Same order as before the redesign: saved games keep their settings.
    fields = [base_entry, duration_entry, core_entry, vram_c_entry]
    trigger_fields = [gpu_w_entry, cpu_entry, hold_entry, grace_entry, smooth_entry, watch_entry]

    # Right column: buttons, what to do now, state and, last, the simulated game.
    buttons = ttk.Frame(right)
    buttons.pack(fill='x', pady=(0, 12))
    state_box = ttk.LabelFrame(right, text='Estado (lecturas simuladas)' if simulate else 'Estado', padding=(12, 8), style='Step.TLabelframe')
    state_box.pack(fill='x')
    # Big notice of what to do now: wait, open the game, or the handoff is under way.
    banner = tk.Label(right, justify='center', padx=10, pady=10, wraplength=340)
    plain_title = root.title()
    def show_phase(phase):
        root.title(plain_title)
        if phase is None:
            banner.configure(text='Pulsa INICIAR.\nNo abras el juego hasta que aquí aparezca «▶ ABRE EL JUEGO AHORA».',
                             bg='#eceff1', fg='#263238', font=('Segoe UI', 11, 'bold'))
        elif phase == 'base':
            text = ('▶  ABRE EL JUEGO AHORA\nLa base está puesta: lánzalo desde Steam o su acceso directo.' if watching
                    else 'BASE ALCANZADA\nNo hay juego vigilado: no habrá relevo.')
            if simulate and watching:
                text += '\nEn simulación: «Abrir juego simulado», abajo.'
            banner.configure(text=text, bg='#1e7d32', fg='#ffffff', font=('Segoe UI', 15, 'bold'))
            root.title('▶ ABRE EL JUEGO AHORA · ' + plain_title)
            root.bell()
        elif phase == 'vigilando':
            banner.configure(text='Vigilando el juego\nGPU Watts no carga nada; si el consumo cae (pantalla de carga), sostendrá la base.',
                             bg='#e0f2f1', fg='#004d40', font=('Segoe UI', 12, 'bold'))
        elif phase == 'sosteniendo':
            banner.configure(text='Consumo bajo (pantalla de carga)\nGPU Watts sostiene la base para el próximo salto.',
                             bg='#ede7f6', fg='#4527a0', font=('Segoe UI', 12, 'bold'))
        elif phase == 'subiendo':
            banner.configure(text='Subiendo a la base…\nTodavía NO abras el juego.', bg='#fff3e0', fg='#8a4b00', font=('Segoe UI', 12, 'bold'))
        else:
            banner.configure(text='Juego detectado\nGPU Watts se va retirando poco a poco.', bg='#e3f2fd', fg='#0d47a1', font=('Segoe UI', 12, 'bold'))
        banner.pack(fill='x', pady=(0, 12), before=state_box)
    reading = tk.StringVar(value='Sin carga')
    temperature_text = tk.StringVar(value='Core: —   VRAM: —')
    status = tk.StringVar(value='Listo. Elige el juego y pulsa INICIAR.')
    watch_text = tk.StringVar(value='')
    summary_text = tk.StringVar(value='')
    logtext = tk.StringVar(value='')
    ttk.Label(state_box, textvariable=reading, font=('Segoe UI', 15, 'bold'), wraplength=340).pack(anchor='w')
    split_text = tk.StringVar(value='')
    split_label = ttk.Label(state_box, textvariable=split_text, font=('Segoe UI', 11, 'bold'), foreground='#0d47a1', wraplength=340)
    split_label.pack(anchor='w', pady=(2, 0))
    tip(split_label, texts['split'])
    ttk.Label(state_box, textvariable=temperature_text, wraplength=340).pack(anchor='w', pady=(4, 8))
    ttk.Label(state_box, textvariable=status, wraplength=340).pack(anchor='w')
    ttk.Label(state_box, textvariable=watch_text, wraplength=340).pack(anchor='w', pady=(8, 0))
    ttk.Label(state_box, textvariable=summary_text, wraplength=340).pack(anchor='w', pady=(8, 0))
    ttk.Label(state_box, textvariable=logtext, wraplength=340, style='Hint.TLabel').pack(anchor='w', pady=(8, 0))
    ttk.Label(right, text='Los W son una media aproximada (~1 s), no un techo instantáneo. GPU Watts no toca voltajes, BIOS ni el límite de potencia.',
              wraplength=360, style='Hint.TLabel').pack(anchor='w', pady=(10, 0))
    if simulate:
        sim_box = ttk.LabelFrame(right, text='SOLO SIMULACIÓN · no existe con la tarjeta real', padding=10, style='Sim.TLabelframe')
        sim_box.pack(fill='x', pady=(12, 0))
        ttk.Label(sim_box, foreground='#b00020', wraplength=340, text='Este recuadro imita un juego para ensayar: ábrelo SOLO cuando arriba salga el aviso verde. '
                  'Con la tarjeta real no aparece: el juego de verdad gasta lo que gasta. Los pasos 1 a 4 de la '
                  'izquierda sí son ajustes reales.').pack(anchor='w')
        sim_row = ttk.Frame(sim_box)
        sim_row.pack(fill='x', pady=(8, 0))
        sim_label = ttk.Label(sim_row, text='W del juego simulado  ⓘ')
        sim_label.pack(side='left')
        sim_watts = ttk.Entry(sim_row, width=6)
        sim_watts.insert(0, '300')
        sim_watts.pack(side='left', padx=(6, 0))
        for widget in (sim_label, sim_watts):
            tip(widget, texts['sim_watts'])
        profile_row = ttk.Frame(sim_box)
        profile_row.pack(fill='x', pady=(6, 0))
        profile_label = ttk.Label(profile_row, text='Tipo de juego  ⓘ')
        profile_label.pack(side='left')
        sim_profile = ttk.Combobox(profile_row, state='readonly', values=list(SIM_PROFILES), width=22)
        sim_profile.set(SIM_PROFILES[0])
        sim_profile.pack(side='left', padx=(6, 0))
        for widget in (profile_label, sim_profile):
            tip(widget, texts['sim_profile'])
        sim_buttons = ttk.Frame(sim_box)
        sim_buttons.pack(fill='x', pady=(8, 0))
        sim_text = tk.StringVar(value='Juego simulado cerrado.')
        def open_sim_game():
            try:
                exe = normalized_process(process_box.get())
                watts = float(sim_watts.get().replace(',', '.'))
                if not exe:
                    raise ValueError('Elige o escribe primero el ejecutable del juego (paso 1).')
                if not math.isfinite(watts) or not 40 <= watts <= 340:
                    raise ValueError('W del juego simulado: entre 40 y 340.')
            except ValueError as exc:
                messagebox.showerror('Simulación', str(exc))
                return
            telemetry.open_game(exe, watts, sim_profile.get())
            sim_text.set(f'Abierto: {exe} · {sim_profile.get().lower()}, ~{watts:.0f} W al jugar.')
        def close_sim_game():
            telemetry.close_game()
            sim_text.set('Juego simulado cerrado.')
        sim_phase = tk.StringVar(value='')
        sim_open = ttk.Button(sim_buttons, text='Abrir juego simulado', command=open_sim_game)
        sim_open.pack(side='left')
        tip(sim_open, texts['sim_open'])
        sim_close = ttk.Button(sim_buttons, text='Cerrar juego simulado', command=close_sim_game)
        sim_close.pack(side='left', padx=6)
        tip(sim_close, texts['sim_close'])
        ttk.Label(sim_box, textvariable=sim_text, wraplength=340).pack(anchor='w', pady=(6, 0))
        ttk.Label(sim_box, textvariable=sim_phase, font=('Segoe UI', 10, 'bold'), wraplength=340).pack(anchor='w', pady=(2, 0))

    def update_sensor_mode(_=None):
        only_core = source_box.get() == only_core_source
        csv = source_box.get() == 'CSV HWiNFO'
        if str(source_box.cget('state')) != 'disabled':
            vram_c_entry.configure(state='disabled' if only_core else 'normal')
        if csv:
            csv_button.pack(side='left', padx=10)
            for widget in csv_widgets:
                widget.grid()
        else:
            csv_button.pack_forget()
            for widget in csv_widgets:
                widget.grid_remove()
        if only_core:
            temperature_text.set('Solo core: VRAM NO VIGILADA; no hay parada por temperatura de memoria.')
        else:
            temperature_text.set('Core: —   VRAM: — (ambos necesarios en este modo)')
    def choose_source(_=None):
        update_sensor_mode()
        try:
            store.set_pref('source', source_box.get())
        except OSError as exc:
            status.set(f'No se pudo recordar la fuente de temperatura: {exc}')
    source_box.bind('<<ComboboxSelected>>', choose_source)
    def update_modes():
        update_load_mode()
        update_trigger_mode()
        update_sensor_mode()
    def current_settings():
        # Kept as typed: they are validated again when INICIAR uses them.
        return {'fields': [e.get() for e in fields], 'mode': mode_box.get(), 'vram_mb': vram_box.get(),
                'trigger': trigger_box.get(), 'trigger_fields': [e.get() for e in trigger_fields],
                'close': close_on_process.get()}
    def apply_settings(saved):
        def put(entry, value):
            entry.configure(state='normal')
            entry.delete(0, 'end')
            entry.insert(0, str(value))
        for entry, value in zip(fields, saved.get('fields', [])):
            put(entry, value)
        if saved.get('mode') in ('Core', 'VRAM', 'Mixto'):
            mode_box.set(saved['mode'])
        if 'vram_mb' in saved:
            put(vram_box, saved['vram_mb'])
        if saved.get('trigger') in MODES:
            trigger_box.set(saved['trigger'])
        for entry, value in zip(trigger_fields, saved.get('trigger_fields', [])):
            put(entry, value)
        if 'close' in saved:
            close_on_process.set(bool(saved['close']))
        update_modes()
    def refresh_games(selected=None):
        saved_order[:] = store.ordered()
        game_box.configure(values=[f"{store.games[exe]['name']}  ·  {exe}" if store.games[exe]['name'] != exe else exe for exe in saved_order])
        if selected in saved_order:
            game_box.current(saved_order.index(selected))
        else:
            game_box.set('')
    def choose_game(_=None):
        if game_box.current() < 0:
            return
        exe = saved_order[game_box.current()]
        game = store.games[exe]
        process_box.set(exe)
        if game['settings']:
            apply_settings(game['settings'])
            status.set(f"Ajustes de {game['name']} cargados. Pulsa INICIAR y después abre el juego.")
        else:
            status.set(f"{game['name']}: sin ajustes guardados todavía; se guardarán al pulsar INICIAR.")
    game_box.bind('<<ComboboxSelected>>', choose_game)
    def forget_game():
        if game_box.current() < 0:
            return
        exe = saved_order[game_box.current()]
        if messagebox.askyesno('Olvidar juego', f"¿Quitar {store.games[exe]['name']} de la lista y sus ajustes?"):
            try:
                store.forget(exe)
            except OSError as exc:
                messagebox.showerror('Juegos', f'No se pudo guardar la lista: {exc}')
            refresh_games()
    forget_button.configure(command=forget_game)
    def open_steam():
        try:
            installed = telemetry.steam_games() if simulate else steam_games()
        except OSError as exc:
            messagebox.showerror('Steam', f'No se pudo leer la biblioteca: {exc}')
            return
        if not installed:
            messagebox.showinfo('Steam', 'No se encuentran juegos de Steam instalados en este equipo.')
            return
        dialog = tk.Toplevel(root)
        dialog.title('Juegos de Steam SIMULADOS (ejemplos inventados, no tu biblioteca)' if simulate else 'Juegos de Steam')
        dialog.geometry('620x460')
        dialog.transient(root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='1. Elige el juego').pack(anchor='w')
        game_list = tk.Listbox(frame, height=9, exportselection=False)
        game_list.pack(fill='both', expand=True)
        for name, _ in installed:
            game_list.insert('end', name)
        ttk.Label(frame, text='2. Elige su .exe (el primero suele ser el juego; ayudantes y redistribuibles al final)').pack(anchor='w', pady=(10, 0))
        exe_list = tk.Listbox(frame, height=6, exportselection=False)
        exe_list.pack(fill='both', expand=True)
        candidates = []
        def show_executables(_=None):
            exe_list.delete(0, 'end')
            candidates.clear()
            if not game_list.curselection():
                return
            folder = installed[game_list.curselection()[0]][1]
            candidates.extend(telemetry.game_executables(folder) if simulate else game_executables(folder))
            for name, size, path in candidates:
                exe_list.insert('end', f'{name}   ({size/1048576:.0f} MB)   {path.parent.relative_to(folder)}')
            if candidates:
                exe_list.selection_set(0)
            else:
                exe_list.insert('end', 'No hay ningún .exe en su carpeta.')
        game_list.bind('<<ListboxSelect>>', show_executables)
        def use_selected():
            if not game_list.curselection() or not exe_list.curselection() or not candidates:
                messagebox.showinfo('Steam', 'Elige un juego y su .exe.', parent=dialog)
                return
            exe = normalized_process(candidates[exe_list.curselection()[0]][0])
            name = installed[game_list.curselection()[0]][0]
            try:
                store.add(exe, name)
            except OSError as exc:
                messagebox.showerror('Juegos', f'No se pudo guardar la lista: {exc}', parent=dialog)
                return
            dialog.destroy()
            refresh_games(exe)
            choose_game()
        ttk.Button(frame, text='Guardar y elegir', command=use_selected).pack(anchor='e', pady=(10, 0))
    steam_button.configure(command=open_steam)
    def browse_exe():
        from tkinter import filedialog
        path = filedialog.askopenfilename(title='Elige el .exe del juego', filetypes=[('Programas', '*.exe')])
        if not path:
            return
        exe = normalized_process(path)
        try:
            store.add(exe, game_name(path))
        except OSError as exc:
            messagebox.showerror('Juegos', f'No se pudo guardar la lista: {exc}')
            return
        refresh_games(exe)
        choose_game()
    browse_button.configure(command=browse_exe)
    stop, messages = threading.Event(), queue.Queue()
    worker = None
    watching = False
    closing = False
    def enable(running):
        start_button.configure(state='disabled' if running else 'normal')
        stop_button.configure(state='normal' if running else 'disabled')
        gpu.configure(state='disabled' if running else 'readonly')
        game_box.configure(state='disabled' if running else 'readonly')
        steam_button.configure(state='disabled' if running else 'normal')
        browse_button.configure(state='disabled' if running else 'normal')
        forget_button.configure(state='disabled' if running else 'normal')
        process_box.configure(state='disabled' if running else 'normal')
        trigger_box.configure(state='disabled' if running else 'readonly')
        for entry in trigger_fields:
            entry.configure(state='disabled' if running else 'normal')
        auto_close_box.configure(state='disabled' if running else 'normal')
        mode_box.configure(state='disabled' if running else 'readonly')
        source_box.configure(state='disabled' if running else 'readonly')
        vram_box.configure(state='disabled' if running else 'normal')
        csv_button.configure(state='disabled' if running else 'normal')
        for selector in column_boxes:
            selector.configure(state='disabled' if running else 'readonly')
        for entry in fields:
            entry.configure(state='disabled' if running else 'normal')
        if not running:
            update_modes()
    def start():
        nonlocal worker, watching
        try:
            values = [float(e.get().replace(',', '.')) for e in fields]
            validate(*values[:3])
            if any(not math.isfinite(v) or not 45 <= v <= 100 for v in values[3:]):
                raise ValueError('Límite VRAM: entre 45 y 100 °C.')
            memory_mb = int(vram_box.get())
            if not 64 <= memory_mb <= 4096:
                raise ValueError('VRAM: entre 64 y 4096 MiB.')
            source = None
            if source_box.get() == 'CSV HWiNFO':
                if not csv_path.get() or any(s.current() < 0 for s in column_boxes):
                    raise ValueError('Elige un CSV activo y sus dos columnas de temperatura.')
                source = CsvSensors(csv_path.get(), [s.current() for s in column_boxes])
            elif simulate:
                source = telemetry.sensors(monitor_vram=source_box.get() != only_core_source)
            else:
                source = NativeSensors(telemetry, telemetry.devices[gpu.current()][0], monitor_vram=source_box.get() != only_core_source)
            typed = process_box.get().strip().strip('"')
            watched = normalized_process(typed)
            process_box.set(watched)  # a pasted path is shown as the name actually watched
            trigger_values = [float(e.get().replace(',', '.')) for e in trigger_fields]
            trigger = Cutover(trigger_box.get(), *trigger_values[:4]) if watched else None
            ramp_seconds, watch_seconds = trigger_values[4], trigger_values[5]
            if not math.isfinite(watch_seconds) or not 0 <= watch_seconds <= 1800:
                raise ValueError('Vigilar tras abrir el juego: entre 0 y 1800 segundos (0 = no vigilar).')
            if not math.isfinite(ramp_seconds) or not .5 <= ramp_seconds <= 30:
                raise ValueError('Suavidad: entre 0,5 y 30 segundos.')
        except (ValueError, OSError, RuntimeError) as exc:
            messagebox.showerror('Valores', str(exc))
            return
        stop.clear()
        enable(True)
        watching = bool(watched)
        status.set('Preparando…')
        summary_text.set('')
        if watched:
            try:
                if any(c in typed for c in '/\\:') and store.games.get(watched, {}).get('name', watched) == watched:
                    store.add(watched, game_name(typed))  # pasted path: name it after its folder
                store.remember(watched, current_settings())
                refresh_games(watched)
            except OSError as exc:
                summary_text.set(f'No se pudieron guardar los ajustes del juego: {exc}')
        simulation = {'make_compute': telemetry.compute, 'processes': telemetry.processes} if simulate else {}
        simulation['watch_seconds'] = watch_seconds if trigger_box.get() == ADAPTIVE_MODE else 0
        worker = threading.Thread(target=run_load, args=(telemetry, gpu.current(), *values[:3], stop, messages, watched, close_on_process.get(), source, *values[3:], mode_box.get(), memory_mb, trigger, ramp_seconds), kwargs=simulation, daemon=True)
        worker.start()
    def halt():
        stop.set()
        status.set('Parada solicitada; retirando gradualmente la carga antes de liberar CUDA…')
    start_button = ttk.Button(buttons, text='INICIAR', command=start, style='Big.TButton')
    start_button.pack(side='left')
    tip(start_button, texts['start'])
    stop_button = ttk.Button(buttons, text='PARAR (Esc)', command=halt, state='disabled')
    stop_button.pack(side='left', padx=10)
    tip(stop_button, texts['stop'])
    force_button = ttk.Button(buttons, text='FORZAR CIERRE', command=lambda: os._exit(0))
    force_button.pack(side='right')
    tip(force_button, texts['force'])
    root.bind('<Escape>', lambda _: halt())
    def close():
        nonlocal closing
        closing = True
        halt()
    root.protocol('WM_DELETE_WINDOW', close)
    def poll():
        while True:
            try:
                kind, value = messages.get_nowait()
            except queue.Empty:
                break
            if kind == 'temperatures':
                memory_text = f'{value[1]:.1f} °C' if value[1] is not None else 'NO VIGILADA'
                temperature_text.set(f'Core: {value[0]:.1f} °C   VRAM: {memory_text}')
            elif kind == 'autoclose':
                close()
            elif kind == 'sample':
                watts, temp, duty, left, state = value
                reading.set(f'{watts:.0f} W  ·  {temp} °C  ·  {left:.1f} s restantes')
                status.set(f'{state} · actividad programada {duty*100:.0f}%')
            elif kind == 'split':
                if value is None:
                    split_text.set('')
                elif value[0] < .5:
                    split_text.set(f'Reparto aprox.: GPU Watts 0 W · todo es del juego (≈ {value[1]:.0f} W)  ⓘ')
                else:
                    split_text.set(f'Reparto aprox.: GPU Watts ≈ {value[0]:.0f} W · juego ≈ {value[1]:.0f} W  ⓘ')
            elif kind == 'phase':
                show_phase(value)
            elif kind == 'watch':
                watch_text.set(value)
            elif kind == 'summary':
                summary_text.set(value)
            elif kind == 'log':
                logtext.set('CSV: ' + value)
            else:
                status.set(value)
                if kind == 'done':
                    show_phase(None)
                    split_text.set('')
                    reading.set('Sin carga generada por GPU Watts')
                    enable(False)
        if closing and (worker is None or not worker.is_alive()):
            telemetry.shutdown()
            root.destroy()
            return
        if simulate:
            phase_now = telemetry.game_phase(time.monotonic())
            sim_phase.set('' if phase_now == 'cerrado' else f'El juego simulado está ahora en: {phase_now}')
        root.after(100, poll)
    # Open ready for the last game used.
    offer_sources()
    update_modes()
    show_phase(None)
    refresh_games(store.last)
    choose_game()
    poll()
    root.mainloop()

def error_log_path():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'GPU_Watts' / 'errores.log'


def report_error(kind, value, trace, parent=None):
    """Keep an unexpected error in errores.log and show it.

    INICIAR.bat starts Python with pyw, without a console, so a traceback
    printed to the console would be lost."""
    import traceback
    text = ''.join(traceback.format_exception(kind, value, trace))
    path = error_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as log:
            log.write(f'--- {time.strftime("%Y-%m-%d %H:%M:%S")}\n{text}\n')
    except OSError:
        pass
    if sys.stderr is not None:
        sys.stderr.write(text)
    try:
        import tkinter as tk
        from tkinter import messagebox
        owner = parent or tk.Tk()
        if parent is None:
            owner.withdraw()
        messagebox.showerror('GPU Watts', f'Error inesperado: {value}\n\nDetalles guardados en:\n{path}', parent=owner)
        if parent is None:
            owner.destroy()
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main(simulate='--simular' in sys.argv[1:])
    except Exception:
        report_error(*sys.exc_info())
