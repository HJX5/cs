import os
import sys
import warnings
import platform
import signal
import shutil
import argparse
from typing import List

# Set environment variables for CUDA performance and TensorFlow logging
if any(arg.startswith('--execution-provider') for arg in sys.argv):
    os.environ['OMP_NUM_THREADS'] = '1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

import torch
import onnxruntime
import tensorflow

import modules.globals
import modules.metadata
import modules.ui as ui
from modules.processors.frame.core import get_frame_processors_modules
from modules.utilities import (
    has_image_extension,
    is_image,
    is_video,
    detect_fps,
    create_video,
    extract_frames,
    get_temp_frame_paths,
    restore_audio,
    create_temp,
    move_temp,
    clean_temp,
    normalize_output_path
)

# Filter warnings
warnings.filterwarnings('ignore', category=FutureWarning, module='insightface')
warnings.filterwarnings('ignore', category=UserWarning, module='torchvision')

# Cross-platform resource management
if platform.system() == 'Darwin' and 'ROCMExecutionProvider' in modules.globals.execution_providers:
    del torch


def parse_args() -> None:
    signal.signal(signal.SIGINT, lambda signal_number, frame: destroy())
    program = argparse.ArgumentParser()
    program.add_argument('-s', '--source', help='Select a source image', dest='source_path')
    program.add_argument('-t', '--target', help='Select a target image or video', dest='target_path')
    program.add_argument('-o', '--output', help='Select output file or directory', dest='output_path')
    program.add_argument('--frame-processor', help='Pipeline of frame processors', dest='frame_processor',
                         default=['face_swapper'], choices=['face_swapper', 'face_enhancer'], nargs='+')
    program.add_argument('--keep-fps', help='Keep original fps', dest='keep_fps', action='store_true', default=False)
    program.add_argument('--keep-audio', help='Keep original audio', dest='keep_audio', action='store_true', default=True)
    program.add_argument('--keep-frames', help='Keep temporary frames', dest='keep_frames', action='store_true', default=False)
    program.add_argument('--many-faces', help='Process every face', dest='many_faces', action='store_true', default=False)
    program.add_argument('--video-encoder', help='Adjust output video encoder', dest='video_encoder', default='libx264',
                         choices=['libx264', 'libx265', 'libvpx-vp9'])
    program.add_argument('--video-quality', help='Adjust output video quality', dest='video_quality', type=int, default=18,
                         choices=range(52), metavar='[0-51]')
    program.add_argument('--max-memory', help='Maximum amount of RAM in GB', dest='max_memory', type=int,
                         default=suggest_max_memory())
    program.add_argument('--execution-provider', help='Execution provider', dest='execution_provider', default=['cpu'],
                         choices=suggest_execution_providers(), nargs='+')
    program.add_argument('--execution-threads', help='Number of execution threads', dest='execution_threads', type=int,
                         default=suggest_execution_threads())
    program.add_argument('-v', '--version', action='version',
                         version=f'{modules.metadata.name} {modules.metadata.version}')

    # Register deprecated args
    program.add_argument('-f', '--face', help=argparse.SUPPRESS, dest='source_path_deprecated')
    program.add_argument('--cpu-cores', help=argparse.SUPPRESS, dest='cpu_cores_deprecated', type=int)
    program.add_argument('--gpu-vendor', help=argparse.SUPPRESS, dest='gpu_vendor_deprecated')
    program.add_argument('--gpu-threads', help=argparse.SUPPRESS, dest='gpu_threads_deprecated', type=int)

    args = program.parse_args()

    modules.globals.source_path = args.source_path
    modules.globals.target_path = args.target_path
    modules.globals.output_path = normalize_output_path(modules.globals.source_path, modules.globals.target_path,
                                                        args.output_path)
    modules.globals.frame_processors = args.frame_processor
    modules.globals.headless = args.source_path or args.target_path or args.output_path
    modules.globals.keep_fps = args.keep_fps
    modules.globals.keep_audio = args.keep_audio
    modules.globals.keep_frames = args.keep_frames
    modules.globals.many_faces = args.many_faces
    modules.globals.video_encoder = args.video_encoder
    modules.globals.video_quality = args.video_quality
    modules.globals.max_memory = args.max_memory
    modules.globals.execution_providers = decode_execution_providers(args.execution_provider)
    modules.globals.execution_threads = args.execution_threads

    # Handle face enhancer tumbler
    modules.globals.fp_ui['face_enhancer'] = 'face_enhancer' in args.frame_processor

    modules.globals.nsfw = False

    # Handle deprecated arguments
    handle_deprecated_args(args)


def handle_deprecated_args(args) -> None:
    """Handle deprecated arguments by translating them to the new format."""
    if args.source_path_deprecated:
        print('\033[33mArgument -f and --face are deprecated. Use -s and --source instead.\033[0m')
        modules.globals.source_path = args.source_path_deprecated
        modules.globals.output_path = normalize_output_path(args.source_path_deprecated, modules.globals.target_path,
                                                            args.output_path)
    if args.cpu_cores_deprecated:
        print('\033[33mArgument --cpu-cores is deprecated. Use --execution-threads instead.\033[0m')
        modules.globals.execution_threads = args.cpu_cores_deprecated
    if args.gpu_vendor_deprecated == 'apple':
        print('\033[33mArgument --gpu-vendor apple is deprecated. Use --execution-provider coreml instead.\033[0m')
        modules.globals.execution_providers = decode_execution_providers(['coreml'])
    if args.gpu_vendor_deprecated == 'nvidia':
        print('\033[33mArgument --gpu-vendor nvidia is deprecated. Use --execution-provider cuda instead.\033[0m')
        modules.globals.execution_providers = decode_execution_providers(['cuda'])
    if args.gpu_vendor_deprecated == 'amd':
        print('\033[33mArgument --gpu-vendor amd is deprecated. Use --execution-provider rocm instead.\033[0m')
        modules.globals.execution_providers = decode_execution_providers(['rocm'])
    if args.gpu_threads_deprecated:
        print('\033[33mArgument --gpu-threads is deprecated. Use --execution-threads instead.\033[0m')
        modules.globals.execution_threads = args.gpu_threads_deprecated


def encode_execution_providers(execution_providers: List[str]) -> List[str]:
    return [provider.replace('ExecutionProvider', '').lower() for provider in execution_providers]


def decode_execution_providers(execution_providers: List[str]) -> List[str]:
    available_providers = onnxruntime.get_available_providers()
    encoded_providers = encode_execution_providers(available_providers)

    selected_providers = [available_providers[encoded_providers.index(req)] for req in execution_providers
                          if req in encoded_providers]

    # Default to CPU if no suitable providers are found
    return selected_providers if selected_providers else ['CPUExecutionProvider']


def suggest_max_memory() -> int:
    return 4 if platform.system().lower() == 'darwin' else 16


def suggest_execution_providers() -> List[str]:
    return encode_execution_providers(onnxruntime.get_available_providers())


def suggest_execution_threads() -> int:
    if 'dml' in modules.globals.execution_providers:
        return 1
    if 'rocm' in modules.globals.execution_providers:
        return 1
    return 8


def limit_resources() -> None:
    # Prevent TensorFlow memory leak
    gpus = tensorflow.config.experimental.list_physical_devices('GPU')
    for gpu in gpus:
        tensorflow.config.experimental.set_memory_growth(gpu, True)

    # Limit memory usage
    if modules.globals.max_memory:
        memory = modules.globals.max_memory * 1024 ** 3
        if platform.system().lower() == 'darwin':
            memory = modules.globals.max_memory * 1024 ** 3
        elif platform.system().lower() == 'windows':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetProcessWorkingSetSize(-1, ctypes.c_size_t(memory), ctypes.c_size_t(memory))
        else:
            import resource
            try:
                soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
                if memory > hard:
                    print(f"Warning: Requested memory limit {memory / (1024 ** 3)} GB exceeds system's hard limit. Setting to maximum allowed {hard / (1024 ** 3)} GB.")
                    memory = hard
                resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
            except ValueError as e:
                print(f"Warning: Could not set memory limit: {e}. Continuing with default limits.")

def release_resources() -> None:
    if 'cuda' in modules.globals.execution_providers:
        torch.cuda.empty_cache()


def pre_check() -> bool:
    if sys.version_info < (3, 9):
        update_status('Python version is not supported - please upgrade to 3.9 or higher.')
        return False
    if not shutil.which('ffmpeg'):
        update_status('ffmpeg is not installed.')
        return False
    if 'cuda' in modules.globals.execution_providers and not torch.cuda.is_available():
        update_status('CUDA is not available. Please check your GPU or CUDA installation.')
        return False
    return True


def update_status(message: str, scope: str = 'DLC.CORE') -> None:
    print(f'[{scope}] {message}')
    if not modules.globals.headless and ui.status_label:
        ui.update_status(message)


def start() -> None:
    for frame_processor in get_frame_processors_modules(modules.globals.frame_processors):
        if not frame_processor.pre_start():
            return

    # Process image to image
    if has_image_extension(modules.globals.target_path):
        process_image_to_image()
        return

    # Process image to video
    process_image_to_video()


def process_image_to_image() -> None:
    if modules.globals.nsfw:
        from modules.predicter import predict_image
        if predict_image(modules.globals.target_path):
            destroy(to_quit=False)
            update_status('Processing to image ignored!')
            return

    try:
        shutil.copy2(modules.globals.target_path, modules.globals.output_path)
    except Exception as e:
        print("Error copying file:", str(e))

    for frame_processor in get_frame_processors_modules(modules.globals.frame_processors):
        update_status('Processing...', frame_processor.NAME)
        frame_processor.process_image(modules.globals.source_path, modules.globals.output_path, modules.globals.output_path)
        release_resources()

    if is_image(modules.globals.target_path):
        update_status('Processing to image succeeded!')
    else:
        update_status('Processing to image failed!')


def process_image_to_video() -> None:
    if modules.globals.nsfw:
        from modules.predicter import predict_video
        if predict_video(modules.globals.target_path):
            destroy(to_quit=False)
            update_status('Processing to video ignored!')
            return

    update_status('Creating temporary resources...')
    create_temp(modules.globals.target_path)
    update_status('Extracting frames...')
    extract_frames(modules.globals.target_path)
    temp_frame_paths = get_temp_frame_paths(modules.globals.target_path)
    for frame_processor in get_frame_processors_modules(modules.globals.frame_processors):
        update_status('Processing...', frame_processor.NAME)
        frame_processor.process_video(modules.globals.source_path, temp_frame_paths)
        release_resources()

    handle_video_fps()
    handle_video_audio()
    clean_temp(modules.globals.target_path)

    if is_video(modules.globals.target_path):
        update_status('Processing to video succeeded!')
    else:
        update_status('Processing to video failed!')


def handle_video_fps() -> None:
    if modules.globals.keep_fps:
        update_status('Detecting fps...')
        fps = detect_fps(modules.globals.target_path)
        update_status(f'Creating video with {fps} fps...')
        create_video(modules.globals.target_path, fps)
    else:
        update_status('Creating video with 30.0 fps...')
        create_video(modules.globals.target_path)


def handle_video_audio() -> None:
    if modules.globals.keep_audio:
        if modules.globals.keep_fps:
            update_status('Restoring audio...')
        else:
            update_status('Restoring audio might cause issues as fps are not kept...')
        restore_audio(modules.globals.target_path, modules.globals.output_path)
    else:
        move_temp(modules.globals.target_path, modules.globals.output_path)


def destroy(to_quit=True) -> None:
    if modules.globals.target_path:
        clean_temp(modules.globals.target_path)
    if to_quit: quit()


def run() -> None:
    try:
        parse_args()
        if not pre_check():
            return
        for frame_processor in get_frame_processors_modules(modules.globals.frame_processors):
            if not frame_processor.pre_check():
                return
        limit_resources()
        if modules.globals.headless:
            start()
        else:
            window = ui.init(start, destroy)
            window.mainloop()
    except Exception as e:
        print(f"UI initialization failed: {str(e)}")
        update_status(f"UI initialization failed: {str(e)}")
        destroy()  # Ensure any resources are cleaned up on failure
