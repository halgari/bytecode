import dis
import inspect
import opcode as _opcode
import struct
import types

# alias to keep the 'bytecode' variable free
import bytecode as _bytecode
from bytecode.instr import (UNSET, Instr, Label, SetLineno,
                            const_key, _check_lineno)


ARG_MAX = 2147483647


def _set_docstring(code, consts):
    if not consts:
        return
    first_const = consts[0]
    if isinstance(first_const, str):
        code.docstring = first_const
    elif first_const is None:
        code.docstring = first_const


class ConcreteInstr(Instr):
    """Concrete instruction, inherit from Instr.

    arg must be an integer in the range 0..2147483647.

    It has a read-only size attribute.
    """

    __slots__ = ('_size',)

    def __init__(self, name, arg=UNSET, *, lineno=None):
        self.set(name, arg, lineno=lineno)

    def _check(self, name, arg, lineno):
        super()._check(name, arg, lineno)

        opcode = _opcode.opmap[name]
        if opcode >= _opcode.HAVE_ARGUMENT:
            if arg is UNSET:
                raise ValueError("operation %s requires an argument" % name)

            if isinstance(arg, int):
                # FIXME: it looks like assemble_emit() allows negative argument
                # (minimum=-2147483648)
                if not(0 <= arg <= ARG_MAX):
                    raise ValueError("arg must be in range 0..%s" % ARG_MAX)
            else:
                raise TypeError("arg must be an int")
        else:
            if arg is not UNSET:
                raise ValueError("operation %s has no argument" % name)

    def set(self, name, arg=UNSET, *, lineno=None):
        """Modify the instruction in-place.

        Replace name, arg and lineno attributes.

        This method must be used if the current and new operation don't have
        the same requirements for argument. For example, replacing LOAD_CONST
        with NOP cannot be done with instr.name='NOP' since this change raises
        an exception (operation NOP has no argument).
        """
        super().set(name, arg, lineno=lineno)
        size = 1
        if arg is not UNSET:
            size += 2
            if arg > 0xffff:
                size += 3
        self._size = size

    @property
    def size(self):
        return self._size

    def get_jump_target(self, instr_offset):
        if self._opcode in _opcode.hasjrel:
            return instr_offset + self._size + self._arg
        if self._opcode in _opcode.hasjabs:
            return self._arg
        return None

    def extended_arg(self):
        """Does the argument need to emit a EXTENDED_ARG instruction?"""
        return (self._arg > 0xffff)

    def assemble(self):
        if self._arg is UNSET:
            return struct.pack('<B', self._opcode)

        arg = self._arg
        if arg > 0xffff:
            return struct.pack('<BHBH',
                               _opcode.EXTENDED_ARG, arg >> 16,
                               self._opcode, arg & 0xffff)
        else:
            return struct.pack('<BH', self._opcode, arg)

    @classmethod
    def disassemble(cls, lineno, code, offset):
        op = code[offset]
        if op >= _opcode.HAVE_ARGUMENT:
            arg = code[offset + 1] + code[offset + 2] * 256
        else:
            arg = UNSET
        name = _opcode.opname[op]
        return cls(name, arg, lineno=lineno)


class ConcreteBytecode(_bytecode.BaseBytecode, list):
    def __init__(self):
        super().__init__()
        self.consts = []
        self.names = []
        self.varnames = []

    def copy(self):
        obj = ConcreteBytecode()
        obj._copy_attr_from(self)
        for instr in self:
            obj.append(instr.copy())
        obj.consts = list(self.consts)
        obj.names = list(self.names)
        obj.varnames = list(self.varnames)
        return obj

    def __repr__(self):
        return '<ConcreteBytecode instr#=%s>' % len(self)

    def __eq__(self, other):
        if type(self) != type(other):
            return False

        const_keys1 = list(map(const_key, self.consts))
        const_keys2 = list(map(const_key, other.consts))
        if const_keys1 != const_keys2:
            return False

        if self.names != other.names:
            return False
        if self.varnames != other.varnames:
            return False

        return super().__eq__(other)

    @staticmethod
    def from_code(code, *, extended_arg=False):
        line_starts = dict(dis.findlinestarts(code))

        # find block starts
        instructions = []
        offset = 0
        lineno = code.co_firstlineno
        while offset < len(code.co_code):
            if offset in line_starts:
                lineno = line_starts[offset]

            instr = ConcreteInstr.disassemble(lineno, code.co_code, offset)

            instructions.append(instr)
            offset += instr.size

        # replace jump targets with blocks
        if not extended_arg:
            extended_arg = None
            index = 0
            while index < len(instructions):
                instr = instructions[index]

                if instr.name == 'EXTENDED_ARG' and not extended_arg:
                    if extended_arg is not None:
                        raise ValueError("EXTENDED_ARG followed "
                                         "by EXTENDED_ARG")
                    extended_arg = instr.arg
                    del instructions[index]
                    continue

                if extended_arg is not None:
                    arg = (extended_arg << 16) + instr.arg
                    extended_arg = None

                    instr = ConcreteInstr(instr.name, arg, lineno=instr.lineno)
                    instructions[index] = instr

                index += 1

            if extended_arg is not None:
                raise ValueError("EXTENDED_ARG at the end of the code")

        bytecode = ConcreteBytecode()
        bytecode.name = code.co_name
        bytecode.filename = code.co_filename
        bytecode.flags = code.co_flags
        bytecode.argcount = code.co_argcount
        bytecode.kw_only_argcount = code.co_kwonlyargcount
        bytecode._stacksize = code.co_stacksize
        bytecode.first_lineno = code.co_firstlineno
        bytecode.names = list(code.co_names)
        bytecode.consts = list(code.co_consts)
        bytecode.varnames = list(code.co_varnames)
        bytecode.freevars = list(code.co_freevars)
        bytecode.cellvars = list(code.co_cellvars)
        _set_docstring(bytecode, code.co_consts)

        bytecode[:] = instructions
        return bytecode

    def _assemble_code(self):
        offset = 0
        code_str = []
        linenos = []
        lineno = self.first_lineno
        for instr in self:
            code_str.append(instr.assemble())
            # if instr.lineno is not set, it's inherited from the previous
            # instruction, or from self.first_lineno
            if instr.lineno is not None:
                lineno = instr.lineno
            linenos.append((offset, lineno))
            offset += instr.size
        code_str = b''.join(code_str)
        return (code_str, linenos)

    @staticmethod
    def _assemble_lnotab(first_lineno, linenos):
        lnotab = []
        old_offset = 0
        old_lineno = first_lineno
        for offset, lineno in linenos:
            dlineno = lineno - old_lineno
            if dlineno == 0:
                continue
            old_lineno = lineno

            doff = offset - old_offset
            old_offset = offset

            while doff > 255:
                lnotab.append(b'\xff\x00')
                doff -= 255

            while dlineno < -127:
                lnotab.append(struct.pack('Bb', 0, -127))
                dlineno -= -127

            while dlineno > 126:
                lnotab.append(struct.pack('Bb', 0, 126))
                dlineno -= 126

            assert 0 <= doff <= 255
            assert -127 <= dlineno <= 126

            lnotab.append(struct.pack('Bb', doff, dlineno))

        return b''.join(lnotab)

    def to_code(self):
        code_str, linenos = self._assemble_code()
        lnotab = self._assemble_lnotab(self.first_lineno, linenos)
        nlocals = len(self.varnames)
        return types.CodeType(self.argcount,
                              self.kw_only_argcount,
                              nlocals,
                              # FIXME: compute stack size
                              self._stacksize,
                              self.flags,
                              code_str,
                              tuple(self.consts),
                              tuple(self.names),
                              tuple(self.varnames),
                              self.filename,
                              self.name,
                              self.first_lineno,
                              lnotab,
                              tuple(self.freevars),
                              tuple(self.cellvars))

    def to_concrete_bytecode(self):
        return self

    def to_bytecode(self):
        # find jump targets
        jump_targets = set()
        offset = 0
        for instr in self:
            target = instr.get_jump_target(offset)
            if target is not None:
                jump_targets.add(target)
            offset += instr.size

        # create labels
        jumps = []
        instructions = []
        labels = {}
        offset = 0

        for instr in self:
            if offset in jump_targets:
                label = Label()
                labels[offset] = label
                instructions.append(label)

            jump_target = instr.get_jump_target(offset)
            size = instr.size

            arg = instr.arg
            # FIXME: better error reporting
            if instr.op in _opcode.hasconst:
                arg = self.consts[arg]
            elif instr.op in _opcode.haslocal:
                arg = self.varnames[arg]
            elif instr.op in _opcode.hasname:
                arg = self.names[arg]
            # FIXME: hasfree
            # FIXME: COMPARE_OP operator

            instr = Instr(instr.name, arg, lineno=instr.lineno)
            instructions.append(instr)
            offset += size

            if jump_target is not None:
                jumps.append((instr, jump_target))

        # replace jump targets with blocks
        for instr, jump_target in jumps:
            # FIXME: better error reporting on missing label
            instr.arg = labels[jump_target]

        bytecode = _bytecode.Bytecode()
        bytecode._copy_attr_from(self)

        nargs = bytecode.argcount + bytecode.kw_only_argcount
        if bytecode.flags & inspect.CO_VARARGS:
            nargs += 1
        if bytecode.flags & inspect.CO_VARKEYWORDS:
            nargs += 1
        bytecode.argnames = self.varnames[:nargs]
        _set_docstring(bytecode, self.consts)

        bytecode.extend(instructions)
        return bytecode

    def to_bytecode_blocks(self):
        return self.to_bytecode().to_bytecode_blocks()


class _ConvertCodeToConcrete:
    def __init__(self, code):
        self.bytecode = code
        self.consts = {}
        self.names = []
        self.varnames = []

    def add_const(self, value):
        key = const_key(value)
        if key in self.consts:
            return self.consts[key]
        index = len(self.consts)
        self.consts[key] = index
        return index

    @staticmethod
    def add(names, name):
        try:
            index = names.index(name)
        except ValueError:
            index = len(names)
            names.append(name)
        return index

    def concrete_instructions(self):
        use_blocks = isinstance(self.bytecode, _bytecode.BytecodeBlocks)

        if use_blocks:
            blocks = self.bytecode
        else:
            blocks = (self.bytecode,)

        # outer loop to recompute jumps if needed
        extended_jumps = set()

        # FIXME: don't recompute *everything* when jumps need to be recomputed
        # FIXME: limit to two iterations max
        need_recompute_jumps = True
        while need_recompute_jumps:
            need_recompute_jumps = False

            # convert abstract instructions to concrete instructions
            targets = {}
            jumps = []
            instructions = []
            offset = 0
            lineno = self.bytecode.first_lineno

            for block in blocks:
                if use_blocks:
                    label = block.label
                    targets[label] = offset

                for index, instr in enumerate(block):
                    if isinstance(instr, Label):
                        targets[instr] = offset
                        continue

                    if isinstance(instr, SetLineno):
                        lineno = instr.lineno
                        continue

                    if isinstance(instr, ConcreteInstr):
                        # keep the concrete instruction unchanged
                        pass
                    elif isinstance(instr, Instr):
                        if instr.lineno is not None:
                            lineno = instr.lineno

                        arg = instr.arg
                        is_jump = isinstance(arg, Label)
                        if is_jump:
                            label = arg
                            if use_blocks:
                                jump_key = (block.label, index)
                            else:
                                jump_key = index
                            # fake value, real value is set in the second loop
                            if jump_key in extended_jumps:
                                # this jump requires a EXTENDED_ARG
                                arg = 0x10000
                            else:
                                arg = 0
                        elif instr.op in _opcode.hasconst:
                            arg = self.add_const(arg)
                        elif instr.op in _opcode.haslocal:
                            arg = self.add(self.varnames, arg)
                        elif instr.op in _opcode.hasname:
                            arg = self.add(self.names, arg)

                        instr = ConcreteInstr(instr.name, arg, lineno=lineno)
                        if is_jump:
                            jumps.append((offset, instr, label, jump_key))
                    else:
                        raise ValueError("expect Instr, got %s"
                                         % instr.__class__.__name__)

                    instructions.append(instr)
                    offset += instr.size

            # fix argument of jump instructions: resolve labels
            for instr_offset, instr, label, jump_key in jumps:
                offset = targets[label]
                if instr.op in _opcode.hasjrel:
                    offset = offset - (instr_offset + instr.size)

                if offset > 0xffff and not instr.extended_arg():
                    extended_jumps.add(jump_key)
                    need_recompute_jumps = True

                # FIXME: reject negative offset?
                # (ex: JUMP_FORWARD arg must be positive)
                # ConcreteInstr._set_arg() already rejects negative argument

                instr.arg = offset

        return instructions

    def to_concrete_bytecode(self):
        first_const = self.bytecode.docstring
        if first_const is not UNSET:
            self.add_const(first_const)

        self.varnames.extend(self.bytecode.argnames)

        instructions = self.concrete_instructions()

        consts = [None] * len(self.consts)
        for item, index in self.consts.items():
            # const_key(value)[1] is value: see const_key() function
            consts[index] = item[1]

        concrete = ConcreteBytecode()
        concrete._copy_attr_from(self.bytecode)
        concrete.consts = consts
        concrete.names = self.names
        concrete.varnames = self.varnames

        # copy instructions
        concrete[:] = instructions
        return concrete
