"""Render MilkDrop presets off-screen and mux the frames with the original audio."""
import argparse
import ctypes as C
import ctypes.util
import importlib.util
import os
import shutil
from fractions import Fraction
from pathlib import Path
import sys

WIDTH, HEIGHT, FPS = 640, 360, 25
RATE = 48000
CHUNK = RATE // FPS
DATA = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'channel-tidemark'
ENGINE = DATA / 'milkdrop-engine'
LACTEA = DATA / 'lactea-engine' / 'liblactea_capi.so'
PRESETS = DATA / 'presets'


def projectm_library():
    local = ENGINE / 'lib' / 'libprojectM-4.so'
    return str(local) if local.is_file() else ctypes.util.find_library('projectM-4')


def engine_available(engine='projectm'):
    library = lactea_library() if engine == 'lactea' else projectm_library()
    return bool(library and audio_available())


def audio_available():
    return bool(importlib.util.find_spec('av') or (ENGINE / 'python' / 'av' / '__init__.py').is_file())


def lactea_dependencies():
    packages = []
    if not audio_available():
        packages.append('python3-av')
    if not ctypes.util.find_library('vulkan'):
        packages.extend(('libvulkan1', 'mesa-vulkan-drivers'))
    if not shutil.which('curl'):
        packages.append('curl')
    return packages


def lactea_library():
    library = Path(os.environ.get('TIDEMARK_LACTEA_LIBRARY') or LACTEA)
    return str(library) if library.is_file() else None


def preset_files(engine='projectm'):
    if not PRESETS.is_dir():
        return []
    extensions = {'.milk', '.milk2'} if engine == 'lactea' else {'.milk'}
    return sorted((path for path in PRESETS.iterdir() if path.is_file() and path.suffix.lower() in extensions),
                  key=lambda path: path.name.casefold())


def import_preset(path):
    if path.suffix.lower() not in {'.milk', '.milk2'} or not 0 < path.stat().st_size <= 2 * 1024 * 1024:
        raise ValueError('Choose a nonempty .milk or .milk2 preset file up to 2 MiB.')
    content = path.read_bytes()
    PRESETS.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = PRESETS / path.name
    for index in range(1, 1001):
        try:
            with target.open('xb') as stream:
                stream.write(content)
            return target
        except FileExistsError:
            if target.read_bytes() == content:
                return target
            target = PRESETS / f'{path.stem}-{index + 1}{path.suffix}'
    raise OSError('The preset library already contains too many files with that name.')


def bind(library, name, result, *args):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = args
    return function


class EglSurface:
    """Create an OpenGL core pbuffer without X11 or a Wayland session."""
    NONE = 0x3038

    def __init__(self):
        handle, number, boolean = C.c_void_p, C.c_int, C.c_uint
        numbers = C.POINTER(number)
        egl = C.CDLL('libEGL.so.1')
        platform = bind(egl, 'eglGetPlatformDisplay', handle, C.c_uint, handle, handle)
        initialize = bind(egl, 'eglInitialize', boolean, handle, numbers, numbers)
        bind_api = bind(egl, 'eglBindAPI', boolean, C.c_uint)
        choose = bind(egl, 'eglChooseConfig', boolean, handle, numbers, C.POINTER(handle), number, numbers)
        pbuffer = bind(egl, 'eglCreatePbufferSurface', handle, handle, handle, numbers)
        context = bind(egl, 'eglCreateContext', handle, handle, handle, handle, numbers)
        self.make_current = bind(egl, 'eglMakeCurrent', boolean, handle, handle, handle, handle)
        self.destroy_context = bind(egl, 'eglDestroyContext', boolean, handle, handle)
        self.destroy_surface = bind(egl, 'eglDestroySurface', boolean, handle, handle)
        self.terminate = bind(egl, 'eglTerminate', boolean, handle)
        self.display = platform(0x31DD, None, None)  # EGL_PLATFORM_SURFACELESS_MESA
        self.context = self.surface = None
        if not self.display or not initialize(self.display, None, None):
            raise RuntimeError('Could not initialize off-screen EGL.')
        try:
            if not bind_api(0x30A2):  # EGL_OPENGL_API
                raise RuntimeError('EGL could not select desktop OpenGL.')
            config, count = handle(), number()
            attrs = self.attributes(0x3033, 1, 0x3040, 8, 0x3024, 8, 0x3023, 8, 0x3022, 8)
            if not choose(self.display, attrs, C.byref(config), 1, C.byref(count)) or not count.value:
                raise RuntimeError('No EGL pbuffer configuration was found.')
            self.surface = pbuffer(self.display, config, self.attributes(0x3057, WIDTH, 0x3056, HEIGHT))
            self.context = context(self.display, config, None, self.attributes(0x3098, 3, 0x30FB, 3, 0x30FD, 1))
            if not self.surface or not self.context or not self.make_current(
                    self.display, self.surface, self.surface, self.context):
                raise RuntimeError('Could not create an OpenGL 3.3 context.')
        except RuntimeError:
            self.close()
            raise

    @classmethod
    def attributes(cls, *values):
        return (C.c_int * (len(values) + 1))(*values, cls.NONE)

    def close(self):
        self.make_current(self.display, None, None, None)
        if self.context:
            self.destroy_context(self.display, self.context)
        if self.surface:
            self.destroy_surface(self.display, self.surface)
        self.terminate(self.display)


class Renderer:
    pixel_format = 'rgb24'
    width, height = WIDTH, HEIGHT
    fps = FPS

    def __init__(self, presets, textures):
        path = projectm_library()
        if not path:
            raise RuntimeError('Install the projectM 4 engine from the MilkDrop tab.')
        self.surface = EglSurface()
        self.instance = None
        try:
            gl = C.CDLL('libGL.so.1')
            self.read_pixels = bind(gl, 'glReadPixels', None, C.c_int, C.c_int, C.c_int, C.c_int,
                                    C.c_uint, C.c_uint, C.c_void_p)
            pixel_store = bind(gl, 'glPixelStorei', None, C.c_uint, C.c_int)
            pixel_store(0x0D05, 1)  # GL_PACK_ALIGNMENT
            handle = C.c_void_p
            library = C.CDLL(path)
            create = bind(library, 'projectm_create', handle)
            self.destroy = bind(library, 'projectm_destroy', None, handle)
            set_size = bind(library, 'projectm_set_window_size', None, handle, C.c_size_t, C.c_size_t)
            set_fps = bind(library, 'projectm_set_fps', None, handle, C.c_int32)
            self.load = bind(library, 'projectm_load_preset_file', None, handle, C.c_char_p, C.c_bool)
            self.render = bind(library, 'projectm_opengl_render_frame', None, handle)
            self.audio = bind(library, 'projectm_pcm_add_int16', None, handle, C.POINTER(C.c_int16),
                              C.c_uint, C.c_int)
            self.instance = create()
            if not self.instance:
                raise RuntimeError('projectM could not create a renderer.')
            set_size(self.instance, WIDTH, HEIGHT)
            set_fps(self.instance, FPS)
            if textures:
                texture_paths = bind(library, 'projectm_set_texture_search_paths', None, handle,
                                     C.POINTER(C.c_char_p), C.c_size_t)
                texture_paths(self.instance, (C.c_char_p * 1)(os.fsencode(textures)), 1)
            self.pixels = C.create_string_buffer(WIDTH * HEIGHT * 3)
            self.presets = presets
        except (OSError, AttributeError, RuntimeError):
            self.close()
            raise

    def choose(self, index, smooth=False):
        if self.presets:
            self.load(self.instance, os.fsencode(self.presets[index]), smooth)

    def frame(self, pcm, samples):
        pcm = (C.c_int16 * (samples * 2)).from_buffer_copy(pcm)
        self.audio(self.instance, pcm, samples, 2)
        self.render(self.instance)
        self.read_pixels(0, 0, WIDTH, HEIGHT, 0x1907, 0x1401, self.pixels)  # GL_RGB, GL_UNSIGNED_BYTE
        pixels = memoryview(self.pixels).cast('B')
        stride = WIDTH * 3
        return b''.join(pixels[y * stride:(y + 1) * stride] for y in range(HEIGHT - 1, -1, -1))

    def close(self):
        if self.instance:
            self.destroy(self.instance)
            self.instance = None
        self.surface.close()


class LacteaConfig(C.Structure):
    # Public C ABI in Lactea v0.2.2's lactea.h.
    _fields_ = [
        ('width', C.c_uint32), ('height', C.c_uint32),
        ('mesh_x', C.c_uint32), ('mesh_y', C.c_uint32), ('seed', C.c_uint64),
        ('compat', C.c_int32), ('sample_rate', C.c_uint32), ('fps', C.c_uint32),
        ('preset_duration', C.c_double), ('auto_hard_cut_mode', C.c_uint8),
    ]


class LacteaRenderer:
    pixel_format = 'rgba'
    width, height, fps = 256, 144, 10

    def __init__(self, presets, textures, interval):
        path = lactea_library()
        if not path:
            raise RuntimeError('Install Lactea from the MilkDrop tab.')
        library = C.CDLL(path)
        handle = C.c_void_p
        status = C.c_int32
        self.error = bind(library, 'lactea_last_error', C.c_char_p)
        default = bind(library, 'lactea_config_default', None, C.POINTER(LacteaConfig))
        create = bind(library, 'lactea_create', handle, C.POINTER(LacteaConfig))
        self.destroy = bind(library, 'lactea_destroy', None, handle)
        self.load = bind(library, 'lactea_load_preset_ex', status, handle, C.c_char_p,
                         C.c_size_t, C.c_char_p, C.c_double)
        self.audio = bind(library, 'lactea_push_pcm_i16', status, handle, C.c_void_p, C.c_size_t, C.c_uint32)
        self.render = bind(library, 'lactea_render_to_buffer', status, handle, C.c_double,
                           C.c_uint32, C.c_void_p, C.c_size_t)
        config = LacteaConfig()
        default(C.byref(config))
        config.width, config.height = self.width, self.height
        config.compat = 2  # LACTEA_COMPAT_MILKDROP3
        config.sample_rate, config.fps = RATE, self.fps
        config.preset_duration = interval
        self.instance = create(C.byref(config))
        if not self.instance:
            self.check(-1)
        try:
            if textures:
                set_textures = bind(library, 'lactea_set_texture_dir', status, handle, C.c_char_p)
                self.check(set_textures(self.instance, os.fsencode(textures)))
            if not presets:
                idle = bind(library, 'lactea_load_idle_preset', status, handle)
                self.check(idle(self.instance))
            self.pixels = C.create_string_buffer(self.width * self.height * 4)
            self.presets = presets
            self.frame_index = 0
        except (OSError, AttributeError, RuntimeError):
            self.close()
            raise

    def check(self, status):
        if status:
            detail = self.error()
            raise RuntimeError(detail.decode('utf-8', errors='replace') if detail else 'Lactea could not render.')

    def choose(self, index, smooth=False):
        if self.presets:
            path = Path(self.presets[index])
            data = path.read_bytes()
            self.check(self.load(self.instance, data, len(data), os.fsencode(path.name), 2.0 if smooth else 0.0))

    def frame(self, pcm, samples):
        audio = (C.c_int16 * (samples * 2)).from_buffer_copy(pcm)
        self.check(self.audio(self.instance, audio, samples, 2))
        self.check(self.render(self.instance, self.frame_index / self.fps, self.frame_index,
                               self.pixels, len(self.pixels)))
        self.frame_index += 1
        return self.pixels

    def close(self):
        if self.instance:
            self.destroy(self.instance)
            self.instance = None


def render_stream(renderer, interval):
    if not importlib.util.find_spec('av'):
        sys.path.append(str(ENGINE / 'python'))
    import av

    fps = renderer.fps
    samples_per_frame = RATE // fps
    with av.open(sys.stdout.buffer, mode='w', format='nut') as output:
        video = output.add_stream('rawvideo', rate=fps)
        video.width, video.height, video.pix_fmt = renderer.width, renderer.height, renderer.pixel_format
        audio = output.add_stream('pcm_s16le', rate=RATE)
        audio.layout = 'stereo'
        frame_index = audio_index = 0
        current_preset = -1
        while pcm := sys.stdin.buffer.read(samples_per_frame * 4):
            samples = len(pcm) // 4
            if not samples:
                break
            pcm = pcm[:samples * 4]
            if renderer.presets:
                preset = frame_index // (interval * fps) % len(renderer.presets)
                if preset != current_preset:
                    renderer.choose(preset, smooth=frame_index > 0)
                    current_preset = preset
            image = av.VideoFrame(renderer.width, renderer.height, renderer.pixel_format)
            image.planes[0].update(renderer.frame(pcm, samples))
            image.pts, image.time_base = frame_index, Fraction(1, fps)
            audio_frame = av.AudioFrame(format='s16', layout='stereo', samples=samples)
            audio_frame.planes[0].update(pcm)
            audio_frame.sample_rate = RATE
            audio_frame.pts, audio_frame.time_base = audio_index, Fraction(1, RATE)
            for packet in video.encode(image) + audio.encode(audio_frame):
                output.mux(packet)
            frame_index += 1
            audio_index += samples
        for stream in (video, audio):
            for packet in stream.encode():
                output.mux(packet)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['projectm', 'lactea'], default='projectm')
    parser.add_argument('--preset', action='append', default=[])
    parser.add_argument('--textures', default='')
    parser.add_argument('--interval', type=int, default=30)
    parser.add_argument('--check', action='store_true')
    options = parser.parse_args()
    if not engine_available(options.engine):
        parser.error(f'{options.engine} and python3-av are required.')
    try:
        interval = max(1, options.interval)
        if options.engine == 'lactea':
            renderer = LacteaRenderer(options.preset, options.textures, interval)
        else:
            renderer = Renderer(options.preset, options.textures)
        try:
            if options.check:
                renderer.choose(0)
                renderer.frame(bytes(CHUNK * 4), CHUNK)
            else:
                render_stream(renderer, interval)
        finally:
            renderer.close()
    except (OSError, RuntimeError, AttributeError, ValueError) as error:
        parser.exit(1, f'MilkDrop: {error}\n')


if __name__ == '__main__':
    main()
