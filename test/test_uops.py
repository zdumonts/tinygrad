from typing import Optional, Tuple, Any, List
import unittest, math
import numpy as np
from tinygrad.tensor import Tensor, _to_np_dtype
from tinygrad.helpers import CI, DEBUG, getenv, Context
from tinygrad.dtype import dtypes, DType, PtrDType
from tinygrad.device import Buffer, Device
from tinygrad.ops import UOps, NOp, UOp, UnaryOps, BinaryOps, TernaryOps, ReduceOps, KernelInfo, exec_alu # noqa F401
from tinygrad.renderer import Program
from tinygrad.engine.schedule import create_schedule
from tinygrad.engine.realize import CompiledRunner, lower_schedule_item, get_kernel
from tinygrad.codegen.uopgraph import linearize_uop
from test.helpers import is_dtype_supported, TestUOps as TestEqUOps

def _uops_to_prg(uops_list):
  uops = linearize_uop(uops_list, extra_pm=Device[Device.DEFAULT].renderer.extra_matcher)
  src = Device[Device.DEFAULT].renderer.render("test", uops)
  has_local = Device[Device.DEFAULT].renderer.has_local
  return CompiledRunner(Program("test", src, Device.DEFAULT, uops=uops,
                                global_size=[1,1,1] if has_local else None, local_size=[1,1,1] if has_local else None))

def uop(uops:List[UOp], uop:UOps, dtype:Optional[DType], src:Tuple[UOp, ...], arg:Any=None) -> UOp:
  uops.append(UOp(uop, dtype, tuple(src), arg))
  return uops[-1]

def _test_single_value(vals, op, dts):
  uops = []
  output_dtype = dts[-1] if op is TernaryOps.WHERE else dtypes.bool if op is BinaryOps.CMPLT else dts[0]
  buf_store = uop(uops, UOps.DEFINE_GLOBAL, PtrDType(output_dtype), (), 0)
  buf_loads = [uop(uops, UOps.DEFINE_GLOBAL, PtrDType(dtype), (), i+1) for i,dtype in enumerate(dts)]
  loads = (uop(uops, UOps.LOAD, dtype, [buf_loads[i], uop(uops, UOps.CONST, dtypes.int32, (), 0)]) for i,dtype in enumerate(dts))
  alu = uop(uops, UOps.ALU, output_dtype, loads, op)
  out = uop(uops, UOps.STORE, None, (buf_store, uop(uops, UOps.CONST, dtypes.int32, (), 0), alu))
  buf = Buffer(Device.DEFAULT, 1, output_dtype).allocate()
  buf2 = [Buffer(Device.DEFAULT, 1, dtype).allocate().copyin(np.array([a], dtype=_to_np_dtype(dtype)).data) for a,dtype in zip(vals, dts)]
  prg = _uops_to_prg([out])
  prg.exec([buf]+buf2)
  ret = np.empty(1, _to_np_dtype(output_dtype))
  buf.copyout(ret.data)
  return ret[0]

def _test_single_value_const(vals, op, dts):
  uops = []
  output_dtype = dts[-1] if op is TernaryOps.WHERE else dtypes.bool if op is BinaryOps.CMPLT else dts[0]
  buf_store = uop(uops, UOps.DEFINE_GLOBAL, PtrDType(output_dtype), (), 0)
  loads = (uop(uops, UOps.CONST, dtype, [], a) for a,dtype in zip(vals, dts))
  alu = uop(uops, UOps.ALU, output_dtype, loads, op)
  out = uop(uops, UOps.STORE, None, (buf_store, uop(uops, UOps.CONST, dtypes.int32, (), 0), alu))
  buf = Buffer(Device.DEFAULT, 1, output_dtype).allocate()
  prg = _uops_to_prg([out])
  prg.exec([buf])
  ret = np.empty(1, _to_np_dtype(output_dtype))
  buf.copyout(ret.data)
  return ret[0]

def _test_uops_result(output_dtype, uops, res):
  # uops = []
  buf_store = uop(uops, UOps.DEFINE_GLOBAL, PtrDType(output_dtype), (), 0)
  # res = output_fn(uops)
  out = uop(uops, UOps.STORE, None, (buf_store, uop(uops, UOps.CONST, dtypes.int32, (), 0), res))
  buf = Buffer(Device.DEFAULT, 1, output_dtype).allocate()
  prg = _uops_to_prg([out])
  prg.exec([buf])
  ret = np.empty(1, _to_np_dtype(output_dtype))
  buf.copyout(ret.data)
  return ret[0]

class TestUOps(unittest.TestCase):
  def _equal(self, v1, v2):
    assert isinstance(v2, (float, int, bool))
    if isinstance(v2, float):
      np.testing.assert_allclose(v1, v2, rtol=2e-7)
    else:
      np.testing.assert_equal(v1, v2)

  def _test_uop_fxn(self, op, fxn, dts=(dtypes.float32, )):
    for f in [_test_single_value, _test_single_value_const]:
      for a in [-2.0, 0.0, 1.0]:
        a = dtypes.as_const(a, dts[0])
        self._equal(f([a], op, dts), fxn(a))

  def _test_bop_fxn(self, op, fxn, dts=(dtypes.float32, )*2, no_b_zero=False, no_b_neg=False):
    for f in [_test_single_value, _test_single_value_const]:
      for a in [-2.0, 0.0, 1.0]:
        for b in [-3.0, 1.0] + ([] if no_b_zero else [0.0]):
          a = dtypes.as_const(a, dts[0])
          b = dtypes.as_const(abs(b) if no_b_neg else b, dts[1])
          self._equal(f([a,b], op, dts), fxn(a,b))

  def _test_top_fxn(self, op, fxn, dts=(dtypes.float32, )*3):
    for f in [_test_single_value, _test_single_value_const]:
      for a in [-2.0, 0, 1]:
        for b in [-3.0, 3.0]:
          for c in [-4.0, 4.0]:
            a = dtypes.as_const(a, dts[0])
            b = dtypes.as_const(b, dts[1])
            c = dtypes.as_const(c, dts[2])
            self._equal(f([a,b,c], op, dts), fxn(a,b,c))

class TestFloatUOps(TestUOps):
  def test_neg(self): self._test_uop_fxn(UnaryOps.NEG, lambda a: -a)
  @unittest.skipIf(Device.DEFAULT == "CLANG", 'not supported as uop')
  def test_exp2(self): self._test_uop_fxn(UnaryOps.EXP2, lambda a: np.exp2(a))
  @unittest.skipIf(Device.DEFAULT == "CLANG", 'not supported as uop')
  def test_log2(self): self._test_uop_fxn(UnaryOps.LOG2, lambda a: math.log2(a) if a > 0 else float('-inf' if a==0 else 'nan'))
  @unittest.skipIf(Device.DEFAULT == "CLANG", 'not supported as uop')
  def test_sin(self): self._test_uop_fxn(UnaryOps.SIN, lambda a: math.sin(a))
  def test_recip(self): self._test_uop_fxn(UnaryOps.RECIP, lambda a: 1/a if a != 0 else float('inf'))
  def test_sqrt(self): self._test_uop_fxn(UnaryOps.SQRT, lambda a: math.sqrt(a) if a >= 0 else float('nan'))

  def test_add(self): self._test_bop_fxn(BinaryOps.ADD, lambda a,b: a+b)
  def test_mul(self): self._test_bop_fxn(BinaryOps.MUL, lambda a,b: a*b)
  def test_max(self): self._test_bop_fxn(BinaryOps.MAX, lambda a,b: max(a,b))
  def test_cmplt(self): self._test_bop_fxn(BinaryOps.CMPLT, lambda a,b: a<b)
  # MOD isn't tested on floats

  def test_where(self):
    self._test_top_fxn(TernaryOps.WHERE, lambda a,b,c: b if a!=0 else c, (dtypes.bool, dtypes.float, dtypes.float))

  @unittest.skipUnless(getenv("PYTHON"), "only python supports MULACC")
  def test_mulacc(self):
    self._test_top_fxn(TernaryOps.MULACC, lambda a,b,c: a*b+c, (dtypes.float, dtypes.float, dtypes.float))

class TestNonFloatUOps(TestUOps):
  def test_neg_int32(self): self._test_uop_fxn(UnaryOps.NEG, lambda a: -a, (dtypes.int32, ))
  def test_add_int32(self): self._test_bop_fxn(BinaryOps.ADD, lambda a,b: int(a)+int(b), (dtypes.int32, dtypes.int32))
  def test_mul_int32(self): self._test_bop_fxn(BinaryOps.MUL, lambda a,b: int(a)*int(b), (dtypes.int32, dtypes.int32))
  @unittest.skipUnless(getenv("PTX"), "only ptx uses bitshifts")
  def test_shr_int32(self): self._test_bop_fxn(BinaryOps.SHR, lambda a,b: int(a)>>int(b), (dtypes.int32, dtypes.int32), no_b_neg=True)
  @unittest.skipUnless(getenv("PTX"), "only ptx uses bitshifts")
  def test_shl_int32(self): self._test_bop_fxn(BinaryOps.SHL, lambda a,b: int(a)<<int(b), (dtypes.int32, dtypes.int32), no_b_neg=True)
  def test_div_int32(self):
    self._test_bop_fxn(BinaryOps.IDIV, lambda a,b: int(a/b), (dtypes.int32, dtypes.int32), no_b_zero=True)
  def test_and_int32(self): self._test_bop_fxn(BinaryOps.AND, lambda a,b: int(a)&int(b), (dtypes.int32, dtypes.int32))
  def test_or_int32(self): self._test_bop_fxn(BinaryOps.OR, lambda a,b: int(a)|int(b), (dtypes.int32, dtypes.int32))
  def test_mod_int32(self):
    self._test_bop_fxn(BinaryOps.MOD,
                       lambda a,b: abs(int(a))%abs(int(b))*(1,-1)[a<0], (dtypes.int32, dtypes.int32), no_b_zero=True)
  def test_cmplt_int32(self): self._test_bop_fxn(BinaryOps.CMPLT, lambda a,b: float(a<b), (dtypes.int32, dtypes.int32))
  @unittest.skipUnless(is_dtype_supported(dtypes.bool), "dtype not supported")
  def test_mul_bool(self): self._test_bop_fxn(BinaryOps.MUL, lambda a,b: bool(a) and bool(b), (dtypes.bool, dtypes.bool))
  @unittest.skipUnless(is_dtype_supported(dtypes.float16), "dtype not supported")
  def test_where_float16(self):
    self._test_top_fxn(TernaryOps.WHERE, lambda a,b,c: b if a!=0 else c, (dtypes.bool, dtypes.float16, dtypes.float16))

class TestBoolUOps(TestUOps):
  def _test_uop_bool_fxn(self, op, fxn):
    for f in [_test_single_value, _test_single_value_const]:
      for a in [False, True]:
        self._equal(f([a], op, (dtypes.bool, )*1), fxn(a))

  def _test_bop_bool_fxn(self, op, fxn):
    for f in [_test_single_value, _test_single_value_const]:
      for a in [False, True]:
        for b in [False, True]:
          self._equal(f([a,b], op, (dtypes.bool, )*2), fxn(a,b))

  def _test_top_bool_fxn(self, op, fxn):
    for f in [_test_single_value, _test_single_value_const]:
      for a in [False, True]:
        for b in [False, True]:
          for c in [False, True]:
            self._equal(f([a,b,c], op, (dtypes.bool, )*3), fxn(a,b,c))

  def test_not_bool(self): self._test_uop_bool_fxn(UnaryOps.NEG, lambda a: not a)
  def test_add_bool(self): self._test_bop_bool_fxn(BinaryOps.ADD, lambda a,b: a or b)
  def test_mul_bool(self): self._test_bop_bool_fxn(BinaryOps.MUL, lambda a,b: a and b)
  def test_xor_bool(self): self._test_bop_bool_fxn(BinaryOps.XOR, lambda a,b: a != b)
  def test_and_bool(self): self._test_bop_bool_fxn(BinaryOps.AND, lambda a,b: a & b)
  def test_or_bool(self): self._test_bop_bool_fxn(BinaryOps.OR, lambda a,b: a | b)
  def test_cmpne_bool(self): self._test_bop_bool_fxn(BinaryOps.CMPNE, lambda a,b: a != b)
  def test_cmplt_bool(self): self._test_bop_bool_fxn(BinaryOps.CMPLT, lambda a,b: a < b)
  def test_where_bool(self): self._test_top_bool_fxn(TernaryOps.WHERE, lambda a,b,c: b if a else c)

class TestExecALU(TestUOps):
  def test_sqrt(self):
    self.assertEqual(exec_alu(UnaryOps.SQRT, dtypes.float, (0.0,)), 0.0)

  def test_div(self):
    self.assertEqual(exec_alu(BinaryOps.IDIV, dtypes.int8, (8, 2)), 4)
    self.assertEqual(exec_alu(BinaryOps.IDIV, dtypes.int8, (7, 3)), 2)
    self.assertEqual(exec_alu(BinaryOps.IDIV, dtypes.int8, (7, -3)), -2)
    self.assertEqual(exec_alu(BinaryOps.IDIV, dtypes.int8, (-50, 6)), -8)

    np.testing.assert_allclose(exec_alu(BinaryOps.MUL, dtypes.float32, (7.0, exec_alu(UnaryOps.RECIP, dtypes.float32, (3.0,)))), 2+(1.0/3.0))
    np.testing.assert_allclose(exec_alu(BinaryOps.MUL, dtypes.float32, (7.0, exec_alu(UnaryOps.RECIP, dtypes.float32, (-3.0,)))), -2-(1.0/3.0))

  def test_recip(self):
    np.testing.assert_allclose(exec_alu(UnaryOps.RECIP, dtypes.float32, (8,)), 1/8)
    np.testing.assert_allclose(exec_alu(UnaryOps.RECIP, dtypes.float32, (7,)), 1/7)
    np.testing.assert_allclose(exec_alu(UnaryOps.RECIP, dtypes.float32, (-3,)), 1/-3)
    np.testing.assert_allclose(exec_alu(UnaryOps.RECIP, dtypes.float32, (-50,)), 1/-50)

    np.testing.assert_allclose(exec_alu(UnaryOps.RECIP, dtypes.float32, ((32+521+3),)), 1/(32+521+3))
    np.testing.assert_allclose(exec_alu(UnaryOps.RECIP, dtypes.float32, ((34**2),)), 1/(34**2))
    np.testing.assert_allclose(exec_alu(UnaryOps.RECIP, dtypes.float32, (10,)), 1/10)

  def test_bool_neg(self):
    self.assertEqual(exec_alu(UnaryOps.NEG, dtypes.bool, (False,)), True)
    self.assertEqual(exec_alu(UnaryOps.NEG, dtypes.bool, (True,)), False)

  def test_bool_cmplt(self):
    self.assertEqual(exec_alu(BinaryOps.CMPLT, dtypes.bool, (False, False)), False)
    self.assertEqual(exec_alu(BinaryOps.CMPLT, dtypes.bool, (False, True)), True)
    self.assertEqual(exec_alu(BinaryOps.CMPLT, dtypes.bool, (True, False)), False)
    self.assertEqual(exec_alu(BinaryOps.CMPLT, dtypes.bool, (True, True)), False)

  def test_bool_where(self):
    self.assertEqual(exec_alu(TernaryOps.WHERE, dtypes.bool, (False, False, False)), False)
    self.assertEqual(exec_alu(TernaryOps.WHERE, dtypes.int, (False, 2, 4)), 4)
    np.testing.assert_allclose(exec_alu(TernaryOps.WHERE, dtypes.float, (False, 2.2, 4.5)), 4.5)

  def test_overflow(self):
    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.uint8, (250, 250)), 244)
    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.uint8, (256, 0)), 0)
    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.uint8, (0, -1)), 255)
    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.uint8, (0, -1000)), 24)

    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.int8, (127, 0)), 127)
    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.int8, (-128, 0)), -128)
    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.int8, (-100, -100)), 56)
    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.int8, (-1000, -0)), 24)
    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.int8, (-130, -0)), 126)

    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.int8, (1, 1)), 2)
    self.assertEqual(exec_alu(BinaryOps.ADD, dtypes.int8, (-128, 0)), -128)

class TestConstantFolding(unittest.TestCase):
  def test_cast_const(self):
    t = Tensor(1, dtype=dtypes.float).cast(dtypes.int)
    si = create_schedule([t.lazydata])
    assert len(si) == 0

  def test_bitcast_const(self):
    t = Tensor(1, dtype=dtypes.float).bitcast(dtypes.int)
    si = create_schedule([t.lazydata])
    assert len(si) == 1
    ji = lower_schedule_item(si[-1])
    assert any(uop.op is UOps.BITCAST for uop in ji.prg.p.uops), f"{[uop.op for uop in ji.prg.p.uops]} does not contain bitcast"

class TestGatedStoreRewrite(unittest.TestCase):
  @unittest.expectedFailure
  def test_tiny_gate_store(self):
    gmem = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), (), 0)
    gidx0 = UOp(UOps.SPECIAL, dtypes.int, (), ('gidx0', 4))
    idx = gidx0 * UOp.const(dtypes.int, 2)
    val = UOp.const(dtypes.float, 42.0)
    gate = gidx0.lt(UOp.const(dtypes.int, 1))
    store = UOp(UOps.STORE, None, (gmem, idx, val, gate))
    uops = linearize_uop([store])
    if DEBUG >= 4: print(Device[Device.DEFAULT].renderer.render("test", uops))
    if_uop = next(u for u in uops if u.op is UOps.IF)
    endif = next(u for u in uops if u.op is UOps.ENDIF)
    assert endif.src[0] is if_uop
    gated_uops = tuple(uops.uops[uops.uops.index(if_uop)+1:uops.uops.index(endif)])
    self.assertEqual(len(gated_uops), 1)
    self.assertIs(gated_uops[-1].op, UOps.STORE)

  @unittest.expectedFailure
  def test_gate_some_stores(self):
    gmem0 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), (), 0)
    gmem1 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), (), 1)
    gidx0 = UOp(UOps.SPECIAL, dtypes.int, (), ('gidx0', 4))
    idx = gidx0*UOp.const(dtypes.int, 2)
    val = UOp.const(dtypes.float, 42.0)
    gate = gidx0.lt(UOp.const(dtypes.int, 1))
    stores = [UOp.store(gmem0, idx, val, gate), UOp.store(gmem1, idx, val)]
    uops = linearize_uop(stores)
    if DEBUG >= 4: print(Device[Device.DEFAULT].renderer.render("test", uops))
    if_uop = next(u for u in uops if u.op is UOps.IF)
    endif = next(u for u in uops if u.op is UOps.ENDIF)
    assert endif.src[0] is if_uop
    gated_uops = tuple(uops.uops[uops.uops.index(if_uop)+1:uops.uops.index(endif)])
    self.assertEqual(len(gated_uops), 1)
    self.assertIs(gated_uops[-1].op, UOps.STORE)

  # scaled down version of TestLinearizerDumb.test_unmerged_ifs
  @unittest.expectedFailure
  def test_merge_ifs_alt(self):
    gmem0 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), (), 0)
    gmem1 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), (), 1)
    gidx0 = UOp(UOps.SPECIAL, dtypes.int, (), ('gidx0', 4))
    idx = gidx0*UOp.const(dtypes.int, 2)
    val = UOp.const(dtypes.float, 42.0)
    gate = gidx0.lt(UOp.const(dtypes.int, 1))
    stores = [UOp.store(gmem0, idx, val, gate), UOp.store(gmem1, idx, val, gate)]
    uops = linearize_uop(stores)
    if DEBUG >= 4: print(Device[Device.DEFAULT].renderer.render("test", uops))
    ifs = [u for u in uops if u.op is UOps.IF]
    endifs = [u for u in uops if u.op is UOps.ENDIF]
    self.assertEqual(len(ifs), 1)
    self.assertEqual(len(endifs), 1)
    gated_uops = tuple(uops.uops[uops.uops.index(ifs[0])+1:uops.uops.index(endifs[0])])
    self.assertEqual(len(gated_uops), 2)
    for x in gated_uops: self.assertIs(x.op, UOps.STORE)

class TestLocalAccess(unittest.TestCase):
  # NOTE: this is failing on METAL CI, no idea why. Works locally.
  @unittest.skipIf(Device.DEFAULT == "METAL" and CI, "failing only in CI")
  @unittest.skipUnless(Device[Device.DEFAULT].renderer.has_shared, "test requires shared memory")
  def test_local_basic(self):
    uops = []
    smem = uop(uops, UOps.DEFINE_LOCAL, PtrDType(dtypes.float32), (), ('smem', 16))
    st = uop(uops, UOps.STORE, None, (smem, uop(uops, UOps.CONST, dtypes.int32, (), 0), uop(uops, UOps.CONST, dtypes.float32, (), 42.0)))
    barr = uop(uops, UOps.BARRIER, None, (st,))
    sres = uop(uops, UOps.LOAD, dtypes.float32, (smem, uop(uops, UOps.CONST, dtypes.int32, (), 0), barr))
    self.assertEqual(_test_uops_result(dtypes.float32, uops, sres), 42)

  @unittest.skipUnless(Device[Device.DEFAULT].renderer.has_shared, "test requires shared memory")
  def test_local_indirect(self):
    uops = []
    smem = uop(uops, UOps.DEFINE_LOCAL, PtrDType(dtypes.int32), (), ('smem', 16))
    st1 = uop(uops, UOps.STORE, None, (smem, uop(uops, UOps.CONST, dtypes.int32, (), 1), uop(uops, UOps.CONST, dtypes.int32, (), 2)))
    st2 = uop(uops, UOps.STORE, None, (smem, uop(uops, UOps.CONST, dtypes.int32, (), 2), uop(uops, UOps.CONST, dtypes.int32, (), 42)))
    barr = uop(uops, UOps.BARRIER, None, (st1,st2))
    ofs = uop(uops, UOps.LOAD, dtypes.int32, (smem, uop(uops, UOps.CONST, dtypes.int32, (), 1), barr))
    sres = uop(uops, UOps.LOAD, dtypes.int32, (smem, ofs))
    self.assertEqual(_test_uops_result(dtypes.int32, uops, sres), 42)

@unittest.skipUnless(getenv("PTX"), "This only tests assembly backends")
class TestAssembly(unittest.TestCase):
  def test_bitshift_left(self):
    g1 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int32), (), 0)
    c1 = UOp(UOps.CONST, dtypes.int, (), 2)
    c2 = UOp(UOps.CONST, dtypes.int, (), 3)
    l1 = UOp(UOps.LOAD, dtypes.int, (g1, c1))
    a1 = UOp(UOps.ALU, dtypes.int, (l1, c1), BinaryOps.MUL)
    a2 = UOp(UOps.ALU, dtypes.int, (l1, c2), BinaryOps.MUL)
    uops = linearize_uop([a1,a2], extra_pm=Device[Device.DEFAULT].renderer.extra_matcher)
    Device[Device.DEFAULT].renderer.render("test", uops)
    self.assertEqual(uops[-1].arg, BinaryOps.SHL)
    self.assertEqual(uops[-2].arg, BinaryOps.MUL)

  def test_bitshift_right(self):
    g1 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.int32), (), 0)
    c1 = UOp(UOps.CONST, dtypes.int, (), 2)
    c2 = UOp(UOps.CONST, dtypes.int, (), 3)
    l1 = UOp(UOps.LOAD, dtypes.int, (g1, c1))
    a1 = UOp(UOps.ALU, dtypes.int, (l1, c1), BinaryOps.IDIV)
    a2 = UOp(UOps.ALU, dtypes.int, (l1, c2), BinaryOps.IDIV)
    uops = linearize_uop([a1,a2], extra_pm=Device[Device.DEFAULT].renderer.extra_matcher)
    Device[Device.DEFAULT].renderer.render("test", uops)
    self.assertEqual(uops[-1].arg, BinaryOps.SHR)
    self.assertEqual(uops[-2].arg, BinaryOps.IDIV)

class TestUOpCompare(unittest.TestCase):
  def test_alu_same_src_different_arg(self):
    a = UOp(UOps.CONST, dtypes.float, (), 2.0)
    b = UOp(UOps.CONST, dtypes.float, (), 3.0)

    add = UOp(UOps.ALU, dtypes.float, (a, b), BinaryOps.ADD)
    mul = UOp(UOps.ALU, dtypes.float, (a, b), BinaryOps.MUL)
    assert (add < mul) or (mul < add), "add and mul with same src should have an order"

class TestUOpStr(TestEqUOps):
  def test_uop_str(self):
    a = UOp(UOps.CONST, dtypes.float, (), 2.0) + UOp(UOps.CONST, dtypes.float, (), 3.0)
    for _ in range(20): a = a + a
    assert len(str(a)) < 10_000, "exponential string growth"
    assert str(eval(str(a))) == str(a)

    t = Tensor.arange(10)
    t = t + t * Tensor.rand(10)
    # nice big complicated uop
    with Context(NOOPT=1):
      sink = UOp(UOps.SINK, None, (get_kernel(Device[Device.DEFAULT].renderer, t.schedule()[-1].ast).linearize().uops[-1],))
    self.assert_equiv_uops(sink, eval(str(sink)))

  def test_nop_str(self):
    a = NOp(UOps.CONST, dtypes.float, (), 2.0, name="c0") + NOp(UOps.CONST, dtypes.float, (), 3.0, name="c1")
    assert str(eval(str(a))) == str(a)

class TestIndexingOrdering(unittest.TestCase):
  # NOTE: these tests skip type_verify since they add dtype to STORE
  @unittest.expectedFailure
  def test_simple_order(self):
    buf = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), (), 0)
    st0 = UOp(UOps.STORE, dtypes.float.vec(4), (buf, UOp.const(dtypes.int, 0), UOp.const(dtypes.float.vec(4), 42)))
    st1 = UOp(UOps.STORE, dtypes.float, (buf, UOp.const(dtypes.int, 4), UOp.const(dtypes.float, 10)))
    uops = linearize_uop([st1, st0], skip_check=True)
    stores = [st for st in uops if st.op is UOps.STORE]
    assert stores[0].src[1] < stores[1].src[1], f"stored at idx {stores[1].src[1].arg} AFTER {stores[0].src[1].arg}"

  @unittest.expectedFailure
  def test_ordering_multi_output(self):
    buf0 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), (), 0)
    buf1 = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), (), 1)
    st0_0 = UOp(UOps.STORE, dtypes.float.vec(4), (buf0, UOp.const(dtypes.int, 0), UOp.const(dtypes.float.vec(4), 42)))
    st1_0 = UOp(UOps.STORE, dtypes.float, (buf0, UOp.const(dtypes.int, 4), UOp.const(dtypes.float, 10)))
    st0_1 = UOp(UOps.STORE, dtypes.float.vec(4), (buf1, UOp.const(dtypes.int, 0), UOp.const(dtypes.float.vec(4), 42)))
    st1_1 = UOp(UOps.STORE, dtypes.float, (buf1, UOp.const(dtypes.int, 4), UOp.const(dtypes.float, 10)))
    uops = linearize_uop([st0_0, st1_0, st0_1, st1_1], skip_check=True)
    stores = [st for st in uops if st.op is UOps.STORE]
    print("\n".join(map(str, stores)))
    # buf0 stores come first
    self.assertEqual(stores[0].src[0].arg, stores[1].src[0].arg)
    # buf1 stores come next
    self.assertEqual(stores[2].src[0].arg, stores[3].src[0].arg)
    # both stores are aligned based on idx
    assert stores[0].src[1] < stores[1].src[1], f"stored at idx {stores[1].src[1].arg} AFTER {stores[0].src[1].arg}"
    assert stores[2].src[1] < stores[3].src[1], f"stored at idx {stores[1].src[1].arg} AFTER {stores[0].src[1].arg}"

  def test_simple_order_with_special(self):
    buf = UOp(UOps.DEFINE_GLOBAL, PtrDType(dtypes.float), (), 0)
    gidx0 = UOp(UOps.SPECIAL, dtypes.int, (), ('gidx0', 4))
    st0 = UOp(UOps.STORE, dtypes.float.vec(4), (buf, gidx0+UOp.const(dtypes.int, 0), UOp.const(dtypes.float.vec(4), 42)))
    st1 = UOp(UOps.STORE, dtypes.float, (buf, UOp.const(dtypes.int, 4), UOp.const(dtypes.float, 10)))
    uops = linearize_uop([st1, st0], skip_check=True)
    stores = [st for st in uops if st.op is UOps.STORE]
    assert stores[0].src[1] < stores[1].src[1], f"stored at idx {stores[1].src[1].arg} AFTER {stores[0].src[1].arg}"

if __name__ == '__main__':
  unittest.main(verbosity=2)
