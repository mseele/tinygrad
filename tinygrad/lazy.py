from __future__ import annotations
import sys, math
import numpy as np
from typing import Union, Optional, Any, Tuple, List, Set, Dict
from tinygrad.helpers import prod, dtypes, DType, merge_dicts, flatten, getenv, dedup, ImageDType, DEBUG, all_int
from tinygrad.ops import LoadOps, UnaryOps, BinaryOps, TernaryOps, ReduceOps, BufferOps
from tinygrad.ops import Op, LazyOp, ConstBuffer, MemBuffer, ScheduleItem, vars_from_ast
from tinygrad.shape.symbolic import sint, Variable
from tinygrad.shape.shapetracker import ShapeTracker
from tinygrad.device import Buffer
from tinygrad.graph import log_lazybuffer
from weakref import ref, WeakSet, WeakValueDictionary

# lazy can recurse a lot
sys.setrecursionlimit(10000)

lazycache: WeakValueDictionary = WeakValueDictionary()
def create_lazybuffer(device:str, st:ShapeTracker, dtype:DType,
                      op:Optional[Op]=None, arg:Any=None, srcs:Tuple[LazyBuffer, ...]=(),
                      base:Optional[LazyBuffer]=None):
  if 0 in st.shape: st, op, arg, srcs = ShapeTracker.from_shape(st.shape), LoadOps.CONST, 0, ()

  wop = (device, st, dtype, op, arg, tuple(ref(x) for x in srcs), ref(base) if base else None)
  if wop in lazycache: return lazycache[wop]

  ret = LazyBuffer(device, st, dtype, op, arg, srcs, base=base)
  # TODO: remove LoadOps.CONST here while keeping a pretty graph and working fusions
  # TODO: might be possible to remove LoadOps.COPY
  if op not in {LoadOps.EMPTY, LoadOps.CUSTOM, LoadOps.CONST, LoadOps.COPY} and getenv("LAZYCACHE", 1): lazycache[wop] = ret
  return ret

class LazyBuffer:
  def __init__(self, device:str, st:ShapeTracker, dtype:DType,
               op:Optional[Op]=None, arg:Any=None, srcs:Tuple[LazyBuffer, ...]=(),
               base:Optional[LazyBuffer]=None):
    self.device, self.st, self.dtype = device, st, dtype
    self.shape = self.st.shape
    assert base is None or base.base == base
    assert base is None or len(srcs) == 0
    self._base = base
    self.children: WeakSet[LazyBuffer] = WeakSet()
    for x in srcs: x.base.children.add(self.base)
    self.op, self.arg, self.srcs = op, arg, srcs  # this is a LazyOp, except the src is LazyBuffers and not LazyOps
    self._realized: Optional[Buffer] = None
    self.output_buffer: Optional[Buffer] = None
    self.forced_realize = False
    self.contiguous_child: Optional[Tuple] = None

  def __repr__(self) -> str:
    return f"<LB {self.device} {self.shape} contig:{self.st.contiguous} {self.op} {self.realized}>"

  @property
  def base(self) -> LazyBuffer: return self._base if self._base is not None else self

  @property
  def realized(self): return self.base._realized

  @staticmethod
  def new(device, shape:Tuple[int, ...], dtype:DType, op, arg):
    return create_lazybuffer(device, ShapeTracker.from_shape(shape), dtype.scalar(), op, arg)

  def const(self, val:Union[float, int]) -> LazyBuffer:
    return LazyBuffer.new(self.device, (), self.dtype, LoadOps.CONST, val).reshape((1,)*len(self.shape)).expand(self.shape)

  def contiguous(self):
    if not self.st.contiguous or self.st.size() != self.base.st.size() or self.is_unrealized_const():
      ret = self.e(LoadOps.CONTIGUOUS)
      self.base.contiguous_child = (ref(ret), self.st)
      return ret
    self.base.forced_realize = True
    return self

  def cast(self, dtype:DType, bitcast:bool=False):
    if self.dtype == dtype: return self
    return create_lazybuffer(self.device, ShapeTracker.from_shape(self.shape), dtype, UnaryOps.CAST, (dtype, bitcast), (self,))

  def is_unrealized_const(self): return not self.realized and self.base.op == LoadOps.CONST
  def is_unrealized_contiguous_const(self): return not self.realized and self.op == LoadOps.CONST

  def schedule(self, seen=None): return create_schedule([self], seen)

  @staticmethod
  def fromCPU(x: np.ndarray) -> LazyBuffer:
    ret = LazyBuffer("CPU", ShapeTracker.from_shape(x.shape), dtypes.from_np(x.dtype), op=LoadOps.EMPTY)
    ret._realized = Buffer("CPU", prod(x.shape), dtypes.from_np(x.dtype), x.flatten())
    return ret

  def copy_to_device(self, device:str) -> LazyBuffer:
    # COPY there and back = no COPY at all
    if not self.realized and self.op == LoadOps.COPY and self.srcs[0].device == device: return self.srcs[0]

    # TODO: const doesn't have to be copied (issues with disk tensor)
    #if self.is_unrealized_const(): return self.const(self.base.arg)._view(self.st)
    out = self.contiguous()
    return create_lazybuffer(device, out.st, out.dtype, LoadOps.COPY, srcs=(out,))

  def e(self:LazyBuffer, op:Union[LoadOps, UnaryOps, BinaryOps, TernaryOps], *srcs:LazyBuffer, arg:Optional[Any]=None) -> LazyBuffer:
    srcs = (self,)+srcs
    new_srcs = []
    for s in srcs:
      if s.contiguous_child is not None:
        wroot, st = s.contiguous_child
        root: LazyBuffer = wroot()
        sti = st.invert()
        if root is not None and sti is not None:
          new_srcs.append(root._view(sti))
          continue
      new_srcs.append(s)
    srcs = tuple(new_srcs)
    return create_lazybuffer(self.device, ShapeTracker.from_shape(self.shape), max(x.dtype for x in srcs), op, arg, srcs)

  # *** reduce ops ***

  def _reduce_op(self:LazyBuffer, op:ReduceOps, new_shape:Tuple[sint, ...]) -> LazyBuffer:
    if self.shape == tuple(new_shape): return self
    unbound_new_shape = tuple(s.unbind()[0] if not isinstance(s, int) else s for s in new_shape)
    return create_lazybuffer(self.device, ShapeTracker.from_shape(new_shape), self.dtype, op, unbound_new_shape, (self,))

  def r(self:LazyBuffer, op:ReduceOps, new_shape:Tuple[sint, ...]) -> LazyBuffer:
    # TODO: can we split symbolic shape if the reduce axis is not symbolic?
    if not all_int(self.shape) or (0 in self.shape) or prod(self.shape) // prod(new_shape) < getenv("REDUCEOP_SPLIT_THRESHOLD", 32768):
      return self._reduce_op(op, new_shape)
    heuristic, divisor, dim_to_split = max(((divisor := math.gcd(256, old))/(stride or math.inf), divisor, i) for i, (old, new, stride) in enumerate(zip(self.shape, new_shape, self.st.real_strides())) if old != new) # type: ignore  # noqa: E501
    if divisor < 16 or heuristic < 0.1: return self._reduce_op(op, new_shape)
    # choose largest divisor (>=16) to split on, penalize large strides
    def splitted_shape(dim_aft_div):
      return self.shape[:dim_to_split] + (self.shape[dim_to_split]//divisor,) + dim_aft_div + self.shape[dim_to_split+1:]
    return self.reshape(splitted_shape((divisor,)))._reduce_op(op, splitted_shape((1,))).reshape(splitted_shape(()))._reduce_op(op, new_shape)

  # *** movement ops ***

  def _view(self:LazyBuffer, new_st:ShapeTracker) -> LazyBuffer:
    if new_st.contiguous and self.base.shape == new_st.shape: return self.base
    return create_lazybuffer(self.device, new_st, self.dtype, base=self.base)

  def reshape(self:LazyBuffer, arg:Tuple[sint, ...]) -> LazyBuffer: return self._view(self.st.reshape(arg))
  def pad(self:LazyBuffer, arg:Tuple[Tuple[int, int], ...]) -> LazyBuffer: return self._view(self.st.pad(arg))
  def expand(self:LazyBuffer, arg:Tuple[sint, ...]) -> LazyBuffer: return self._view(self.st.expand(arg))
  def permute(self:LazyBuffer, arg:Tuple[int, ...]) -> LazyBuffer: return self._view(self.st.permute(arg))
  def shrink(self:LazyBuffer, arg:Tuple[Tuple[sint, sint], ...]) -> LazyBuffer: return self._view(self.st.shrink(arg))
  def stride(self:LazyBuffer, arg:Tuple[int, ...]) -> LazyBuffer: return self._view(self.st.stride(arg))

# *** schedule creation ***

# recursively create a lazyop
def _recursive_lazyop(buf:LazyBuffer, inputs:List[LazyBuffer], var_vals:Dict[Variable, int], st:ShapeTracker, realizes:Set[LazyBuffer], first=True):
  if buf != buf.base:
    var_vals.update(merge_dicts([var_vals, buf.st.var_vals]))
    st = buf.st+st
    buf = buf.base
  # all buffers here are base now
  assert buf.op is not None

  # consts are always fused and generated
  if buf.op == LoadOps.CONST:
    return LazyOp(BufferOps.CONST, (), ConstBuffer(float(buf.arg), buf.dtype, st.simplify().unbind()))

  # if we aren't fusing it, it's a load and we add it to the inputs
  if buf.realized or (buf in realizes and not first):
    if buf not in inputs: inputs.append(buf)
    return LazyOp(BufferOps.LOAD, (), MemBuffer(inputs.index(buf)+1, buf.dtype, st.simplify().unbind()))

  # if a CONTIGUOUS made it all the way here, just skip it
  if buf.op == LoadOps.CONTIGUOUS:
    return _recursive_lazyop(buf.srcs[0], inputs, var_vals, st, realizes, False)

  # if it's a reduce, we have to change the shapetracker
  if buf.op in ReduceOps:
    assert st.contiguous, "ReduceOps late fusion must be contiguous"
    st = ShapeTracker.from_shape(buf.srcs[0].shape).unbind()

  # otherwise we fuse it like normal
  return LazyOp(buf.op, tuple(_recursive_lazyop(x, inputs, var_vals, st, realizes, False) for x in buf.srcs), buf.arg)

# recursively walk back in the graph to create the schedule
def _recursive_schedule(out:LazyBuffer, seen:Set[LazyBuffer], realizes:Set[LazyBuffer],
                        reduce_for_op: Dict[LazyBuffer, LazyBuffer]) -> List[ScheduleItem]:
  if out in seen or out.realized or out.op == LoadOps.CONST: return []
  assert out.base == out
  seen.add(out)

  inputs: List[LazyBuffer] = []
  var_vals: Dict[Variable, int] = out.st.var_vals.copy()
  if out.op == LoadOps.COPY:
    op, inputs = LazyOp(LoadOps.COPY, (), out.srcs[0].base), [out.srcs[0].base]
  elif out.op == LoadOps.CUSTOM:
    op, inputs = LazyOp(LoadOps.CUSTOM, (), out.arg), list(out.srcs)
  elif out.op == LoadOps.EMPTY:
    op = LazyOp(LoadOps.EMPTY)
  else:
    output_st = ShapeTracker.from_shape(reduce_for_op[out].shape if out in reduce_for_op else out.shape)
    op = _recursive_lazyop(out, inputs, var_vals, output_st, realizes)
    op = LazyOp(BufferOps.STORE, (op, ), MemBuffer(0, out.dtype, output_st.simplify().unbind()))

  if out.output_buffer is not None:
    for i,a in enumerate(inputs):
      if a.realized == out.output_buffer:
        if any(not x.arg.st.contiguous for x in op.get_lazyops() if x.op == BufferOps.LOAD and x.arg.idx == i+1):
          out.output_buffer = None
          break

  return flatten(_recursive_schedule(x.base, seen, realizes, reduce_for_op) for x in inputs) + \
    [ScheduleItem(op, out, tuple(inputs), {k:var_vals[k] for k in vars_from_ast(op)})]

# recursively search the entire graph for all LazyBuffers, insert realizes after expands
def _recurse_lb(buf:LazyBuffer, realizes:Set[LazyBuffer], allbufs:Dict[LazyBuffer, None], simple_pads:Set[LazyBuffer]):
  if buf in allbufs or buf.realized: return
  log_lazybuffer(buf)
  if isinstance(buf.dtype, ImageDType) and (prod(buf.shape) != prod(buf.dtype.shape) or
                                            not any(buf.shape[x]%4 == 0 for x in buf.st.unit_stride_axes())):
    if DEBUG >= 3: print(f"forcing image {buf.dtype} with shape {buf.shape} to float32")
    buf.dtype = dtypes.float32  # NOTE; this is what makes the dtype above not match
  if buf.forced_realize: realizes.add(buf)
  if buf.base != buf:
    # realize all places where the buffer is expanded
    if prod(buf.base.st.shape) < prod(buf.st.shape):
      if len(buf.st.views) == 1 and buf.st.views[-1].mask and prod(buf.base.st.shape) == prod([y-x for x,y in buf.st.views[-1].mask]):
        simple_pads.add(buf.base)
      else:
        realizes.add(buf.base)
    return _recurse_lb(buf.base, realizes, allbufs, simple_pads)
  allbufs[buf] = None
  if buf.op in LoadOps: realizes.add(buf.base)
  if buf.op == LoadOps.COPY:
    assert buf.srcs[0].st.contiguous and buf.srcs[0].st.size() == buf.srcs[0].base.st.size()
    realizes.add(buf.srcs[0].base)
  for x in buf.srcs: _recurse_lb(x, realizes, allbufs, simple_pads)

UNSAFE_PAD_OPS = {BinaryOps.DIV, BinaryOps.CMPLT, UnaryOps.LOG2, UnaryOps.EXP2, UnaryOps.RECIP}
def _is_padding_okay(buf:LazyBuffer, realizes:Set[LazyBuffer]) -> bool:
  if buf in realizes or buf.realized: return True
  if buf.op in UNSAFE_PAD_OPS: return False
  return all(_is_padding_okay(x.base, realizes) for x in buf.srcs)

def create_schedule(outs:List[LazyBuffer], seen:Optional[Set[LazyBuffer]]=None) -> List[ScheduleItem]:
  if seen is None: seen = set()
  for out in outs: log_lazybuffer(out, scheduled=True)

  # start by just realizing the buffers passed in
  realizes: Set[LazyBuffer] = set([x.base for x in outs if not x.realized])
  allbufs: Dict[LazyBuffer, None] = {}
  simple_pads: Set[LazyBuffer] = set()
  for out in outs: _recurse_lb(out.base, realizes, allbufs, simple_pads)

  # check if we have to realize pads
  for p in simple_pads:
    if not _is_padding_okay(p, realizes):
      realizes.add(p)

  # find all reduces, and pair them to a elementwise op. if they can't be cleanly paired, force realize the reduce (or a contig child)
  reduce_for_op: Dict[LazyBuffer, LazyBuffer] = {}
  for r in allbufs.keys():
    if r != r.base or r.op not in ReduceOps or r in realizes: continue

    # follow the reduce down
    child_set: Dict[LazyBuffer, ShapeTracker] = {r: r.st}
    realized_children: Dict[LazyBuffer, ShapeTracker] = {}
    forced_realize = False
    can_chase = True
    while not forced_realize and len(child_set):
      next_child_set = {}
      for tr,st in child_set.items():
        if tr in realizes:
          realized_children[tr] = st
          # can only have one output buffer
          # can only reduce contiguous
          # max one reduceop per kernel
          if len(realized_children) > 1 or not st.contiguous or st.size() != r.st.size() or (tr in reduce_for_op and reduce_for_op[tr] != r):
            can_chase = tr not in reduce_for_op or reduce_for_op[tr] == r
            forced_realize = True
            break
          continue
        for tr_next in tr.children:
          if not tr_next.realized:
            # max one reduceop per kernel
            if tr_next.op in ReduceOps:
              forced_realize = True
              break
            st_childs = dedup([s for s in tr_next.srcs if s.base == tr])
            if len(st_childs) > 1:
              forced_realize = True
              break
            next_child_set[tr_next] = st + st_childs[0].st
      child_set = next_child_set
    if forced_realize:
      tr = r
      if can_chase:
        # can chase this down to contiguous children
        st = tr.st
        while len(tr.children) == 1:
          tr_next = next(iter(tr.children))
          st_childs = dedup([s for s in tr_next.srcs if s.base == tr])
          if len(st_childs) > 1: break
          if st.size() != st_childs[0].st.size(): break
          st = st + st_childs[0].st
          if not st.contiguous or tr_next.op in ReduceOps: break
          tr = tr_next
        reduce_for_op[tr] = r
      realizes.add(tr)
    else:
      assert len(realized_children) == 1
      reduce_for_op[next(iter(realized_children.keys()))] = r

  return flatten(_recursive_schedule(x.base, seen, realizes, reduce_for_op) for x in outs)