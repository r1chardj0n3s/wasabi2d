from typing import Sequence, Callable
from dataclasses import dataclass

import moderngl
import numpy as np

from .abstract import AbstractAllocator, NoCapacity


TYPE_MAP = {
    'uint8': 'f1',
    'uint16': 'u2',
    'uint32': 'u4',
    'uint64': 'u8',
    'int8': 'i1',
    'int16': 'i2',
    'int32': 'i4',
    'int64': 'i8',
    'float16': 'f2',
    'float32': 'f4',
    'float64': 'f8',
}


def dtype_to_moderngl(dtype: np.dtype):
    """Convert a numpy dtype object to a ModernGL buffer type."""
    names = dtype.names
    assert names is not None, "Only structured numpy dtypes are allowed."
    fields = dtype.fields
    out = []
    byte_pos = 0  # position of next field
    for n in names:
        dtype, offset, *_ = fields[n]

        out.extend('x' * (offset - byte_pos))
        byte_pos = offset + dtype.itemsize

        type_name = TYPE_MAP[dtype.base.name]
        assert len(dtype.shape) <= 1, \
            "Multi-dimensional dtypes are not supported."
        if dtype.shape == ():
            out.append(type_name)
        else:
            out.append(f'{dtype.shape[0]}{type_name}')
    return (' '.join(out), *names)


@dataclass
class List:
    """A list allocated within a VAO."""
    buf: 'VAO'

    # View of the vertex buffer, and slice
    vertbuf: np.ndarray
    vertoff: slice

    # View of the index buffer, and slice
    indexbuf: np.ndarray
    indexoff: slice

    #: True if needs syncing to the GL
    dirty: bool = False

    def free(self):
        self.buf.free(self)


class VAO:
    """Manage vertex lists within a VAO."""

    def __init__(
            self,
            mode: int,
            ctx: moderngl.Context,
            prog: moderngl.Program,
            dtype: np.dtype,
            capacity: int = 4096,
            index_capacity: int = 8192):
        self.mode = mode
        self.ctx = ctx
        self.prog = prog
        self.dtype = dtype
        self.indirect_capacity = 50
        self.allocator = AbstractAllocator(capacity)
        self.index_allocator = AbstractAllocator(index_capacity)
        self.allocs = []
        self._initialise()
        self._initialise_indirect()

    def _initialise(self):
        self.verts = np.zeros(self.allocator.capacity, dtype=self.dtype)
        self.indexes = np.zeros(self.index_allocator.capacity, dtype='i4')

        # Sync allocs into the new buffer
        for a in self.allocs:
            self.verts[a.vertoff] = a.vertbuf
            self.indexes[a.indexoff] = a.indexbuf
            a.vertbuf = self.verts[a.vertoff]
            a.indexbuf = self.indexes[a.indexbuf]
            a.dirty = False

        # Create OpenGL objects
        self.vbo = self.ctx.buffer(self.verts, dynamic=True)
        self.ibo = self.ctx.buffer(self.indexes, dynamic=True)
        self.vao = self.ctx.vertex_array(
            self.prog,
            [
                (self.vbo, *dtype_to_moderngl(self.dtype)),
            ],
            self.ibo
        )

    def _initialise_indirect(self):
        self.indirect = np.zeros((self.indirect_capacity, 5), dtype='u4')
        for aidx, lst in enumerate(self.allocs):
            num_verts = lst.vertoff.stop - lst.vertoff.start
            ixs_start = lst.indexoff.start
            vs_start = lst.vertoff.start
            self.indirect[aidx] = (num_verts, 0, ixs_start, vs_start, 0)
        self.indirectbo = self.ctx.buffer(self.indirect, dynamic=True)
        self.indirect_dirty = False

    def alloc(self, num_verts: int, num_indexes: int) -> List:
        """Allocate a list from within this buffer."""
        try:
            vs = self.allocator.alloc(num_verts)
        except NoCapacity as e:
            self.allocator.grow(e.recommended)
            self._initialise()
            vs = self.allocator.alloc(num_verts)

        try:
            ixs = self.index_allocator.alloc(num_indexes)
        except NoCapacity as e:
            self.index_allocator.grow(e.recommended)
            self._initialise()
            ixs = self.index_allocator.alloc(num_indexes)

        lst = List(
            buf=self,
            vertbuf=self.verts[vs],
            vertoff=vs,
            indexbuf=self.indexes[ixs],
            indexoff=ixs,
            dirty=True
        )
        aidx = len(self.allocs)
        self.allocs.append(lst)

        if len(self.allocs) > self.indirect_capacity:
            self.indirect_capacity *= 2
            self._initialise_indirect()
        else:
            self.indirect[aidx] = (num_indexes, 1, ixs.start, vs.start, 0)
            self.indirect_dirty = True

        return lst

    def free(self, lst):
        """Remove a list from the array."""
        pos = self.allocs.index(lst)

        # Update indirect buffer
        n_allocs = len(self.allocs)
        self.allocs.remove(lst)

        # Move subsequent lists. Caution: linear cost per free
        self.indirect[pos:len(self.allocs)] = self.indirect[pos + 1:n_allocs]
        self.indirect_dirty = True

        # Free space in allocators
        self.allocator.free(lst.vertoff)
        self.index_allocator.free(lst.indexoff)

        lst.buf = None
        lst.vertbuf = None
        lst.indexbuf = None

    def render(self):
        """Render all lists."""
        if not self.allocs:
            return

        dirty = False
        for a in self.allocs:
            if a.dirty:
                dirty = True
                a.dirty = False
        if dirty:
            self.vbo.write(self.verts)
            self.ibo.write(self.indexes)
        if self.indirect_dirty:
            self.indirectbo.write(self.indirect)
            self.indirect_dirty = False

        self.vao.render_indirect(self.indirectbo, mode=self.mode, count=len(self.allocs))

