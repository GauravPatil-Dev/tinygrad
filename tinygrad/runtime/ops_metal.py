from __future__ import annotations
import os, subprocess, pathlib, ctypes, tempfile, functools
from typing import List, Any, Tuple, Optional, cast
from tinygrad.helpers import prod, getenv, T
from tinygrad.device import Compiled, Compiler, CompileError, LRUAllocator
from tinygrad.renderer.cstyle import MetalRenderer

class objc_id(ctypes.c_void_p): # This prevents ctypes from converting response to plain int, and dict.fromkeys() can use it to dedup
  def __hash__(self): return hash(self.value)
  def __eq__(self, other): return self.value == other.value

class objc_instance(objc_id): # method with name "new", "alloc" should be freed after use
  def __del__(self): msg(self, "release")

@functools.lru_cache(None)
def sel(name: str): return libobjc.sel_registerName(name.encode())

class MTLResourceOptions:
  MTLResourceCPUCacheModeDefaultCache = 0
  MTLResourceStorageModeShared = 0 << 4

class MTLPipelineOption:
  MTLPipelineOptionNone = 0

libobjc = ctypes.CDLL("/usr/lib/libobjc.dylib")
libmetal = ctypes.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
# Must be loaded for default Metal Device: https://developer.apple.com/documentation/metal/1433401-mtlcreatesystemdefaultdevice?language=objc
ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
libdispatch = ctypes.CDLL("/usr/lib/libSystem.dylib") # libdispatch is part of libSystem on mac
libobjc.objc_getClass.restype = objc_id
libobjc.sel_registerName.restype = objc_id
libmetal.MTLCreateSystemDefaultDevice.restype = objc_instance
libdispatch.dispatch_data_create.restype = objc_instance

# Ignore mypy error reporting incompatible default, because typevar default only works on python 3.12
def msg(ptr: objc_id, selector: str, /, *args: Any, restype: type[T] = objc_id) -> T: # type: ignore [assignment]
  sender = libobjc["objc_msgSend"] # Using attribute access returns a new reference so setting restype is safe
  sender.restype = restype
  return sender(ptr, sel(selector), *args)

def to_ns_str(s: str): return msg(libobjc.objc_getClass(b"NSString"), "stringWithUTF8String:", s.encode(), restype=objc_instance)

def to_struct(*t: int, _type: type = ctypes.c_ulong):
  class Struct(ctypes.Structure): pass
  Struct._fields_ = [(f"field{i}", _type) for i in range(len(t))]
  return Struct(*t)

def wait_check(cbuf: Any):
  msg(cbuf, "waitUntilCompleted")
  error_check(msg(cbuf, "error", restype=objc_instance))

def elapsed_time(cbuf: objc_id):
  return cast(float, msg(cbuf, "GPUEndTime", restype=ctypes.c_double)) - cast(float, msg(cbuf, "GPUStartTime", restype=ctypes.c_double))

def error_check(error: objc_instance, error_constructor: type[Exception] = RuntimeError):
  if error.value is None: return None
  raise error_constructor(bytes(msg(msg(error, "localizedDescription", restype=objc_instance), "UTF8String", restype=ctypes.c_char_p)).decode())

def metal_src_to_library(device:MetalDevice, src:str) -> objc_instance:
  options = msg(libobjc.objc_getClass(b"MTLCompileOptions"), "new", restype=objc_instance)
  msg(options, "setFastMathEnabled:", getenv("METAL_FAST_MATH"))
  library = msg(device.device, "newLibraryWithSource:options:error:", to_ns_str(src), options,
                ctypes.byref(compileError:=objc_instance()), restype=objc_instance)
  error_check(compileError, CompileError)
  return library

class MetalCompiler(Compiler):
  def __init__(self, device:Optional[MetalDevice]=None):
    self.device = device
    super().__init__("compile_metal_xcode" if self.device is None else "compile_metal")
  def compile(self, src:str) -> bytes:
    if self.device is None:
      # NOTE: if you run llvm-dis on "air" you can see the llvm bytecode
      air = subprocess.check_output(['xcrun', '-sdk', 'macosx', 'metal', '-x', 'metal', '-c', '-', '-o', '-'], input=src.encode('utf-8'))
      lib = subprocess.check_output(['xcrun', '-sdk', 'macosx', 'metallib', '-', '-o', '-'], input=air)
    else:
      library = metal_src_to_library(self.device, src)
      library_contents = msg(library, "libraryDataContents", restype=objc_instance)
      lib = ctypes.string_at(msg(library_contents, "bytes"), cast(int, msg(library_contents, "length", restype=ctypes.c_ulong)))
    assert lib[:4] == b"MTLB", "Invalid Metal library. Using conda? Corrupt XCode?"
    return lib
  def disassemble(self, lib:bytes):
    with tempfile.NamedTemporaryFile(delete=True) as shader:
      shader.write(lib)
      shader.flush()
      ret = os.system(f"cd {pathlib.Path(__file__).parents[2]}/extra/disassemblers/applegpu && python3 compiler_explorer.py {shader.name}")
      if ret: print("Disassembler Error: Make sure you have https://github.com/dougallj/applegpu cloned to tinygrad/extra/disassemblers/applegpu")

class MetalProgram:
  def __init__(self, device:MetalDevice, name:str, lib:bytes):
    self.device, self.name, self.lib = device, name, lib
    if lib[:4] == b"MTLB":
      # binary metal library
      data = libdispatch.dispatch_data_create(lib, len(lib), None, None)
      error_library_creation = objc_instance()
      self.library = msg(self.device.device, "newLibraryWithData:error:", data, ctypes.byref(error_library_creation), restype=objc_instance)
      error_check(error_library_creation)
    else:
      # metal source. rely on OS caching
      try: self.library = metal_src_to_library(self.device, lib.decode())
      except CompileError as e: raise RuntimeError from e
    self.fxn = msg(self.library, "newFunctionWithName:", to_ns_str(name), restype=objc_instance)
    descriptor = msg(libobjc.objc_getClass(b"MTLComputePipelineDescriptor"), "new", restype=objc_instance)
    msg(descriptor, "setComputeFunction:", self.fxn)
    msg(descriptor, "setSupportIndirectCommandBuffers:", True)
    self.pipeline_state = msg(self.device.device, "newComputePipelineStateWithDescriptor:options:reflection:error:",
      descriptor, MTLPipelineOption.MTLPipelineOptionNone, None, ctypes.byref(error_pipeline_creation:=objc_instance()), restype=objc_instance)
    error_check(error_pipeline_creation)

  def __call__(self, *bufs, global_size:Tuple[int,int,int]=(1,1,1), local_size:Tuple[int,int,int]=(1,1,1), vals:Tuple[int, ...]=(), wait=False):
    max_total_threads = msg(self.pipeline_state, "maxTotalThreadsPerThreadgroup", restype=ctypes.c_ulong)
    if prod(local_size) > cast(int, max_total_threads):
      exec_width = msg(self.pipeline_state, "threadExecutionWidth", restype=ctypes.c_ulong)
      memory_length = msg(self.pipeline_state, "staticThreadgroupMemoryLength", restype=ctypes.c_ulong)
      raise RuntimeError(f"local size {local_size} bigger than {max_total_threads} with exec width {exec_width} memory length {memory_length}")
    command_buffer = msg(self.device.mtl_queue, "commandBuffer", restype=objc_instance)
    encoder = msg(command_buffer, "computeCommandEncoder", restype=objc_instance)
    msg(encoder, "setComputePipelineState:", self.pipeline_state)
    for i,a in enumerate(bufs): msg(encoder, "setBuffer:offset:atIndex:", a.buf, a.offset, i)
    for i,a in enumerate(vals,start=len(bufs)): msg(encoder, "setBytes:length:atIndex:", bytes(ctypes.c_int(a)), 4, i)
    msg(encoder, "dispatchThreadgroups:threadsPerThreadgroup:", to_struct(*global_size), to_struct(*local_size))
    msg(encoder, "endEncoding")
    msg(command_buffer, "commit")
    if wait:
      wait_check(command_buffer)
      return elapsed_time(command_buffer)
    self.device.mtl_buffers_in_flight.append(command_buffer)

class MetalBuffer:
  def __init__(self, buf:Any, size:int, offset=0): self.buf, self.size, self.offset = buf, size, offset

class MetalAllocator(LRUAllocator):
  def __init__(self, device:MetalDevice):
    self.device:MetalDevice = device
    super().__init__()
  def _alloc(self, size:int, options) -> MetalBuffer:
    # Buffer is explicitly released in _free() rather than garbage collected via reference count
    ret = msg(self.device.device, "newBufferWithLength:options:", size, MTLResourceOptions.MTLResourceStorageModeShared, restype=objc_id)
    if ret.value is None: raise MemoryError(f"Metal OOM while allocating {size=}")
    return MetalBuffer(ret, size)
  def _free(self, opaque:MetalBuffer, options): msg(opaque.buf, "release")
  def _transfer(self, dest:MetalBuffer, src:MetalBuffer, sz:int, src_dev:MetalDevice, dest_dev:MetalDevice):
    dest_dev.synchronize()
    src_command_buffer = msg(src_dev.mtl_queue, "commandBuffer", restype=objc_instance)
    encoder = msg(src_command_buffer, "blitCommandEncoder", restype=objc_instance)
    msg(encoder, "copyFromBuffer:sourceOffset:toBuffer:destinationOffset:size:", src.buf, ctypes.c_ulong(src.offset),
        dest.buf, ctypes.c_ulong(dest.offset), ctypes.c_ulong(sz))
    msg(encoder, "endEncoding")
    if src_dev != dest_dev:
      msg(src_command_buffer, "encodeSignalEvent:value:", src_dev.timeline_signal, src_dev.timeline_value)
      dest_command_buffer = msg(dest_dev.mtl_queue, "commandBuffer", restype=objc_instance)
      msg(dest_command_buffer, "encodeWaitForEvent:value:", src_dev.timeline_signal, src_dev.timeline_value)
      msg(dest_command_buffer, "commit")
      dest_dev.mtl_buffers_in_flight.append(dest_command_buffer)
      src_dev.timeline_value += 1
    msg(src_command_buffer, "commit")
    src_dev.mtl_buffers_in_flight.append(src_command_buffer)
  def _as_buffer(self, src:MetalBuffer) -> memoryview:
    self.device.synchronize()
    ptr = msg(src.buf, "contents", restype=objc_id) # Shared memory, do not release here
    array = (ctypes.c_char * (src.offset + src.size)).from_address(ptr.value)
    return memoryview(array).cast("B")[src.offset:]
  def _copyin(self, dest:MetalBuffer, src:memoryview): self._as_buffer(dest)[:] = src
  def _copyout(self, dest:memoryview, src:MetalBuffer): dest[:] = self._as_buffer(src)
  def _offset(self, buf:MetalBuffer, size:int, offset:int): return MetalBuffer(buf.buf, size, offset)

class MetalDevice(Compiled):
  def __init__(self, device:str):
    self.device = libmetal.MTLCreateSystemDefaultDevice()
    self.mtl_queue = msg(self.device, "newCommandQueueWithMaxCommandBufferCount:", 1024, restype=objc_instance)
    if self.mtl_queue is None: raise RuntimeError("Cannot allocate a new command queue")
    self.mtl_buffers_in_flight: List[Any] = []
    self.mv_in_metal: List[memoryview] = []
    self.timeline_signal = msg(self.device, "newSharedEvent", restype=objc_instance)
    self.timeline_value = 0

    from tinygrad.runtime.graph.metal import MetalGraph
    super().__init__(device, MetalAllocator(self), MetalRenderer(), MetalCompiler() if getenv("METAL_XCODE") else Compiler(),
                     functools.partial(MetalProgram, self), MetalGraph)
  def synchronize(self):
    for cbuf in self.mtl_buffers_in_flight: wait_check(cbuf)
    self.mv_in_metal.clear()
    self.mtl_buffers_in_flight.clear()
