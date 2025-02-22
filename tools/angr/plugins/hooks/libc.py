#!/usr/bin/env python
#
# Copyright 2019 Carter Yagemann
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import logging
import os

import angr
from angr.procedures.stubs.format_parser import FormatParser
from angr.sim_options import MEMORY_CHUNK_INDIVIDUAL_READS
from angr.storage.memory_mixins.address_concretization_mixin import MultiwriteAnnotation
import claripy
from cle.backends.externs.simdata.io_file import io_file_data_for_arch

log = logging.getLogger(name=__name__)


## Global Constants
WCHAR_BYTES = 4


class libc_clock_gettime(angr.SimProcedure):

    timespec_bits = 16 * 8

    def run(self, clockid, tp):
        if self.state.solver.is_true(tp == 0):
            return -1

        result = {
            'tv_sec': self.state.solver.BVS('tv_sec', self.arch.bits, key=('api', 'clock_gettime', 'tv_sec')),
            'tv_nsec': self.state.solver.BVS('tv_nsec', self.arch.bits, key=('api', 'clock_gettime', 'tv_nsec')),
        }

        self.state.mem[tp].struct.timespec = result
        return 0

class libc___cxa_atexit(angr.SimProcedure):
    def run(self, func):
        # we don't actually care about at_exit callbacks
        return 0


class libc_atol(angr.SimProcedure):
    def handle_symbolic(self, s):
        strtol = angr.SIM_PROCEDURES["libc"]["strtol"]
        ret = strtol.strtol_inner(s, self.state, self.state.memory, 10, True)[1]
        log.debug(
            "atol's return: [%#x-%#x]"
            % (self.state.solver.min(ret), self.state.solver.max(ret))
        )
        return ret

    def run(self, s):
        self.argument_types = {0: self.ty_ptr(angr.sim_type.SimTypeString())}

        if self.state.solver.symbolic(s):
            ret = self.handle_symbolic(s)
        else:
            strlen = self.state.memory.find(s, b"\x00", 256, default=s + 256)[2][0]
            if strlen == 0:
                ret = self.handle_symbolic(s)
            else:
                str = self.state.memory.load(s, strlen)
                str = self.state.solver.eval(str, cast_to=bytes).decode("utf8")
                log.debug("atol concrete string: %s" % str)
                ret = int(str, 10)
                log.debug("atol's return: [%#x]" % ret)

        return ret


class libc_strrchr(angr.SimProcedure):
    def run(self, s_addr, c_int, s_strlen=None):
        """This SimProcedure is a lot looser than angr's strchr, but that's okay
        because we have a concrete trace."""
        Or = self.state.solver.Or
        And = self.state.solver.And

        s_strlen = self.inline_call(angr.SIM_PROCEDURES["libc"]["strlen"], s_addr)
        ret = self.state.solver.BVS("strrchr", 64)

        if self.state.solver.symbolic(s_strlen.ret_expr):
            log.debug("symbolic strlen")
            self.state.add_constraints(
                Or(And(ret >= s_addr, ret < s_addr + s_strlen.max_null_index), ret == 0)
            )
        else:
            log.debug("concrete strlen")
            max_search = self.state.solver.eval(s_strlen.ret_expr) + 1
            self.state.add_constraints(
                Or(And(ret >= s_addr, ret < s_addr + max_search), ret == 0)
            )

        return ret


class libc_gai_strerror(angr.SimProcedure):
    def run(self, errcode):
        err_buf = self.inline_call(angr.SIM_PROCEDURES["libc"]["malloc"], 256).ret_expr
        self.state.memory.store(err_buf + 255, b"\x00")

        return err_buf


class libc_getaddrinfo(angr.SimProcedure):
    def run(self, node, service, hints, res):
        ret = self.state.solver.BVS("getaddrinfo_ret", self.arch.bits)
        return ret


class libc_getenv(angr.SimProcedure):
    def run(self, name):
        Or = self.state.solver.Or
        And = self.state.solver.And

        name_strlen = self.inline_call(angr.SIM_PROCEDURES["libc"]["strlen"], name)
        name_str = self.state.memory.load(name, name_strlen.ret_expr)
        if self.state.solver.symbolic(name_str):
            name_sym = True
            log.debug("getenv: searching for (symbolic)")
        else:
            name_sym = False
            name_str = self.state.solver.eval(name_str, cast_to=bytes).decode("utf8")
            log.debug("getenv: searching for %s" % name_str)

        envpp = self.state.solver.eval(self.state.posix.environ)
        ret_val = self.state.solver.BVS("getenv", self.arch.bits)
        ret_expr = ret_val == 0
        while True:
            try:
                envp = self.state.solver.eval(
                    self.state.memory.load(
                        envpp,
                        self.state.arch.bytes,
                        endness=self.state.arch.memory_endness,
                    )
                )
                if envp == 0:
                    break
                envp_strlen = self.inline_call(
                    angr.SIM_PROCEDURES["libc"]["strlen"], envp
                )
                envp_str = self.state.memory.load(envp, envp_strlen.ret_expr)
                if name_sym or self.state.solver.symbolic(envp_str):
                    ret_expr = Or(
                        ret_expr,
                        And(ret_val > envp, ret_val < (envp + envp_strlen.ret_expr)),
                    )
                else:
                    # we can make the variable concrete
                    envp_str = self.state.solver.eval(envp_str, cast_to=bytes).decode(
                        "utf8"
                    )
                    key = envp_str.split("=")[0]
                    if key == name_str:
                        log.debug("getenv: Found concrete match")
                        return envp + len(key) + 1

                envpp += self.state.arch.bytes
            except Exception as ex:
                log.error("Error in getenv hook: %s" % str(ex))
                break

        self.state.add_constraints(ret_expr)
        return ret_val


class libc_getline(angr.SimProcedure):

    MAX_STRING = 128

    def run(self, lineptr_ptr, n_ptr, stream):
        # warn users that we're making an assumption about max string length
        log.warning("Simulation procedure for getline currently assumes a max"
                " string length of %d bytes" % self.MAX_STRING)

        # free buffer if already allocated
        lineptr = self.state.memory.load(lineptr_ptr, self.state.arch.bits // 8,
                endness=self.state.arch.memory_endness)
        if not self.state.solver.is_true(lineptr == 0):
            self.inline_call(angr.SIM_PROCEDURES["libc"]["free"], lineptr)

        # allocate new buffer
        buf = self.inline_call(angr.SIM_PROCEDURES["libc"]["malloc"],
                self.MAX_STRING).ret_expr
        # place null terminator
        self.state.memory.store(buf + self.MAX_STRING - 1, b"\x00")

        # store it as the new lineptr
        self.state.memory.store(lineptr_ptr, buf, size=self.state.arch.bits // 8,
                endness=self.state.arch.memory_endness)

        # update n_ptr
        n_bv = self.state.solver.BVS("getline_n", 32)
        self.state.add_constraints(n_bv <= self.MAX_STRING)
        self.state.memory.store(n_ptr, n_bv, endness=self.state.arch.memory_endness)

        # generate return value
        ret = self.state.solver.BVS("getline_ret", self.state.arch.bits)
        self.state.add_constraints(ret < self.MAX_STRING)
        return ret


class libc_getlogin(angr.SimProcedure):

    LOGIN_PTR = None

    def run(self):
        if self.LOGIN_PTR is None:
            self.LOGIN_PTR = self.inline_call(
                angr.SIM_PROCEDURES["libc"]["malloc"], 256
            ).ret_expr
            self.state.memory.store(self.LOGIN_PTR + 255, b"\x00")
        return self.LOGIN_PTR


class libc_getpwnam(angr.SimProcedure):

    PASSWD_PTR = None
    CHAR_PTRS = {
        "pw_name": None,
        "pw_paswd": None,
        "pw_gecos": None,
        "pw_dir": None,
        "pw_shell": None,
    }

    def run(self, name):
        malloc = angr.SIM_PROCEDURES["libc"]["malloc"]

        if self.PASSWD_PTR is None:
            # allocate strings
            for ptr in self.CHAR_PTRS:
                self.CHAR_PTRS[ptr] = self.inline_call(malloc, 4096).ret_expr
                self.state.memory.store(self.CHAR_PTRS[ptr] + 4095, b"\x00")

            # allocate passwd struct
            ptr_size = self.state.arch.bytes
            passwd_size = (ptr_size * len(self.CHAR_PTRS)) + 8
            self.PASSWD_PTR = self.inline_call(malloc, passwd_size).ret_expr

            # fill in struct values
            ptr = self.PASSWD_PTR
            for pw_str in ["pw_name", "pw_paswd"]:
                self.state.memory.store(
                    ptr,
                    self.CHAR_PTRS[pw_str],
                    size=ptr_size,
                    endness=self.state.arch.memory_endness,
                )
                ptr += ptr_size
            for pw_sym in ["pw_uid", "pw_gid"]:
                self.state.memory.store(ptr, self.state.solver.BVS(pw_sym, 32))
                ptr += 4
            for pw_str in ["pw_gecos", "pw_dir", "pw_shell"]:
                self.state.memory.store(
                    ptr,
                    self.CHAR_PTRS[pw_str],
                    size=ptr_size,
                    endness=self.state.arch.memory_endness,
                )
                ptr += ptr_size

        return self.PASSWD_PTR

class libc_mbsrtowcs(angr.SimProcedure):

    max_dest_size = 1024

    def run(self, dest, src, len, ps):
        warn_once = True

        # return value is number of wide characters parsed
        ret_val = 0

        # pointer at src is updated to point after last parsed character
        src_base = self.state.memory.load(src, self.state.arch.bytes,
                endness=self.state.arch.memory_endness)

        # determine max number of characters to parse
        len_val = min(self.max_dest_size, self.state.solver.max(len))

        for offset in range(len_val):
            next_byte = self.state.memory.load(src_base + offset, 1)

            if self.state.solver.is_true(next_byte < 0x80):
                # most wide encodings are backwards compatible with ASCII
                ret_val += 1
                if not self.state.solver.is_true(dest == 0):
                    wc = next_byte.zero_extend((WCHAR_BYTES * 8) - 8)
                    self.state.memory.store(dest + (offset * WCHAR_BYTES), wc,
                            endness=self.state.arch.memory_endness)
            else:
                if warn_once:
                    log.warning("mbsrtowcs is assuming source string is all ASCII")
                    warn_once = False

                ret_val += 1
                wc = self.state.solver.BVS('mbsrtowcs_%d' % offset, WCHAR_BYTES * 8)
                self.state.memory.store(dest + (offset * WCHAR_BYTES), wc)

            if self.state.solver.is_true(next_byte == 0):
                # reached and copied null terminator
                break

        # update src
        src_val = src_base + ret_val
        self.state.memory.store(src, src_val, endness=self.state.arch.memory_endness)

        return ret_val

class libc_realpath(angr.SimProcedure):
    MAX_PATH = 4096

    def run(self, path_ptr, resolved_path):

        resolved_path_val = self.state.solver.eval(resolved_path)
        if resolved_path_val == 0:
            buf = self.inline_call(
                angr.SIM_PROCEDURES["libc"]["malloc"], self.MAX_PATH
            ).ret_expr
        else:
            buf = resolved_path

        path_len = self.inline_call(
            angr.SIM_PROCEDURES["libc"]["strlen"], path_ptr
        ).ret_expr
        path_expr = self.state.memory.load(path_ptr, path_len)
        if self.state.solver.symbolic(path_expr):
            self.state.memory.store(
                buf, self.state.solver.BVS("realpath", self.MAX_PATH * 8)
            )
        else:
            cwd = self.state.fs.cwd.decode("utf8")
            path_str = self.state.solver.eval(path_expr, cast_to=bytes).decode("utf8")
            normpath = os.path.normpath(os.path.join(cwd, path_str))[
                : self.MAX_PATH - 1
            ]
            self.state.memory.store(buf, normpath.encode("utf8") + b"\x00")

        return buf

class libc_unlink(angr.SimProcedure):

    def run(self, path_addr):
        strlen = angr.SIM_PROCEDURES['libc']['strlen']

        p_strlen = self.inline_call(strlen, path_addr)
        str_expr = self.state.memory.load(path_addr, p_strlen.max_null_index, endness='Iend_BE')
        str_val = self.state.solver.eval(str_expr, cast_to=bytes)

        # Check if entity exists before attempting to unlink
        if not self.state.fs.get(str_val):
            return -1

        if self.state.fs.delete(str_val):
            return 0
        else:
            return -1

class libc_snprintf(FormatParser):
    """Custom snprintf simproc because angr's doesn't honor the size argument"""

    def run(self, dst_ptr, size):

        if self.state.solver.eval(size) == 0:
            return size

        # The format str is at index 2
        fmt_str = self._parse(2)
        out_str = fmt_str.replace(3, self.arg)

        # enforce size limit
        size = self.state.solver.max(size)
        if (out_str.size() // 8) > size - 1:
            out_str = out_str.get_bytes(0, size - 1)

        # store resulting string
        self.state.memory.store(dst_ptr, out_str)

        # place the terminating null byte
        self.state.memory.store(
            dst_ptr + (out_str.size() // 8), self.state.solver.BVV(0, 8)
        )

        # size_t has size arch.bits
        return self.state.solver.BVV(out_str.size() // 8, self.state.arch.bits)


class libc__fprintf_chk(FormatParser):
    def run(self, stream, flag, fmt):
        # look up stream
        fd_offset = io_file_data_for_arch(self.state.arch)["fd"]
        fileno = self.state.mem[stream + fd_offset :].int.resolved
        simfd = self.state.posix.get_fd(fileno)
        if simfd is None:
            return -1

        # format str is arg index 2
        fmt_str = self._parse(fmt)
        out_str = fmt_str.replace(self.va_arg)

        # write to stream
        simfd.write_data(out_str, out_str.size() // 8)

        return out_str.size() // 8


class libc__snprintf_chk(FormatParser):
    """Custom __snprintf_chk simproc because angr's doesn't honor the size argument"""

    def run(self, s, maxlen, flag, slen, fmt):
        # The format str is at index 4
        fmt_str = self._parse(4)
        out_str = fmt_str.replace(5, self.arg)

        # enforce size limit
        size = self.state.solver.max(slen)
        if (out_str.size() // 8) > slen - 1:
            out_str = out_str.get_bytes(0, max(slen - 1, 1))

        # store resulting string
        self.state.memory.store(s, out_str)

        # place the terminating null byte
        self.state.memory.store(s + (out_str.size() // 8), self.state.solver.BVV(0, 8))

        # size_t has size arch.bits
        return self.state.solver.BVV(out_str.size() // 8, self.state.arch.bits)


class libc_strncat(angr.SimProcedure):
    def run(self, dst, src, num):
        strlen = angr.SIM_PROCEDURES["libc"]["strlen"]
        strncpy = angr.SIM_PROCEDURES["libc"]["strncpy"]
        src_len = self.inline_call(strlen, src).ret_expr
        dst_len = self.inline_call(strlen, dst).ret_expr
        if (src_len > num).is_true():
            max_len = num
        else:
            max_len = src_len
        self.inline_call(strncpy, dst + dst_len, src, max_len + 1, src_len=src_len)
        return dst


class libc_setlocale(angr.SimProcedure):
    locale = None
    def run (self, category, locale):
        if self.locale is None:
            self.locale = self.inline_call(
                angr.SIM_PROCEDURES["libc"]["malloc"], 256
            ).ret_expr
            self.state.memory.store(self.locale + 255, b"\x00")
        return self.locale

class libc_bindtextdomain(angr.SimProcedure):
    domainname = None
    def run (self, domainname, dirname):
        if self.domainname is None:
            self.domainname = self.inline_call(
               angr.SIM_PROCEDURES["libc"]["malloc"], 256
            ).ret_expr
            self.state.memory.store(self.domainname + 255, b"\x00")
        return self.domainname

class libc_textdomain(angr.SimProcedure):
    domainname = None
    def run (self, domainname):
        if self.domainname is None:
            self.domainname = self.inline_call(
               angr.SIM_PROCEDURES["libc"]["malloc"], 256
            ).ret_expr
            self.state.memory.store(self.domainname + 255, b"\x00")
        return self.domainname

class libc_signal(angr.SimProcedure):
    SIG_HNDLR = {}
    def run(self, signum, handler):
        signum_int = self.state.solver.eval(signum)
        if signum_int not in self.SIG_HNDLR:
            self.SIG_HNDLR[signum_int] = self.state.solver.BVV(-1, self.state.arch.bits)
        old = self.SIG_HNDLR[signum_int]
        self.SIG_HNDLR[signum_int] = handler
        return old

class libc_symlink(angr.SimProcedure):

    def run(self, target_ptr, linkpath_ptr):
        # believe it or not, angr currently does not support symlinks, but since
        # our state has an option set to pretend all files exist, we don't have
        # to worry about this
        ret = self.state.solver.BVS("symlink_ret", self.state.arch.bits)
        return ret

class libc_sysconf(angr.SimProcedure):

    def run(self, name):
        return self.state.solver.BVS("sysconf_ret", self.state.arch.bits)

class libc_towupper(angr.SimProcedure):

    def run(self, wc):
        # sizeof(wint_t) == sizeof(wchar_t)
        # Most wide encodings are backwards compatible with ASCII
        if self.state.solver.is_true(wc < 0x80):
            return self.state.solver.If(
                    self.state.solver.And(wc >= 97, wc <= 122),  # a - z
                    wc - 32, wc)
        else:
            log.warning("Simproc towupper cannot handle non-ASCII characters")
            return self.state.solver.BVS("towupper_ret", self.state.arch.bits)

class libc_vfwprintf(angr.SimProcedure):

    def run(self, stream, format, ap):
        log.warning("vfwprintf not implemented, skipping write")
        ret = self.state.solver.BVS("vfwprintf_ret", self.state.arch.bits)
        return ret

class libc_swprintf(angr.SimProcedure):

    def run(self, wcs_ptr, maxlen, format):
        log.warning("swprintf not implemented, symbolizing output string")
        len_val = self.state.solver.max(maxlen)
        log.debug("Symbolizing %d wide characters" % len_val)

        for offset in range(len_val):
            wcs = self.state.solver.BVS('swprintf_%d' % offset, WCHAR_BYTES * 8)
            self.state.memory.store(wcs_ptr + (offset * WCHAR_BYTES), wcs)

        # insert final null character
        null_wcs = self.state.solver.BVV(0, WCHAR_BYTES * 8)
        self.state.memory.store(wcs_ptr + ((len_val - 1) * WCHAR_BYTES), null_wcs)

        # return number of wide characters written
        ret = self.state.solver.BVS('swprintf_ret', self.state.arch.bits)
        self.state.add_constraints(ret <= len_val)
        return ret

class libc_wcschr(angr.SimProcedure):

    max_null_index = 1024

    def run(self, wcs, wc):
        wcs_len = self.inline_call(libc_wcslen, wcs)

        chunk_size = None
        if MEMORY_CHUNK_INDIVIDUAL_READS in self.state.options:
            chunk_size = 1

        if self.state.solver.symbolic(wcs_len.ret_expr):
            log.debug("symbolic wcslen")
            max_sym = min((self.state.solver.max_int(wcs_len.ret_expr) * WCHAR_BYTES) + WCHAR_BYTES,
                    self.state.libc.max_symbolic_strchr)
            a, c, i = self.state.memory.find(wcs, wc, self.max_null_index,
                    max_symbolic_bytes=max_sym, default=0, char_size=WCHAR_BYTES)
        else:
            log.debug("concrete wcslen")
            max_search = (self.state.solver.eval(wcs_len.ret_expr) * WCHAR_BYTES) + WCHAR_BYTES
            a, c, i = self.state.memory.find(wcs, wc, max_search, default=0, chunk_size=chunk_size,
                    char_size=WCHAR_BYTES)

        if len(i) > 1:
            a = a.annotate(MultiwriteAnnotation())
            self.state.add_constraints(*c)

        chrpos = a - wcs
        self.state.add_constraints(self.state.solver.If(a != 0,
                chrpos <= wcs_len.ret_expr * WCHAR_BYTES, True))

        return a

class libc_wcsrchr(angr.SimProcedure):
    def run(self, wcs, wc):
        best_match = None

        wcs_len = self.inline_call(libc_wcslen, wcs)
        max_bytes = self.state.solver.max(wcs_len.ret_expr) * WCHAR_BYTES
        offset = 0

        # convert wc to wchar_t
        if wc.length < (WCHAR_BYTES * 8):
            wc = wc.zero_extend((WCHAR_BYTES * 8) - wc.length)
        elif wc.length > (WCHAR_BYTES * 8):
            wc = wc[31:]
        assert wc.length == (WCHAR_BYTES * 8)
        # flip endness
        if self.state.arch.memory_endness == 'Iend_LE':
            wc = claripy.Concat(*(wc.chop(8)[::-1]))

        while offset < max_bytes:
            a, c, i = self.state.memory.find(wcs + offset, wc, max_bytes - offset,
                    max_symbolic_bytes=128, default=0, char_size=WCHAR_BYTES)
            if self.state.solver.is_true(a == 0):
                break
            best_match = [a, c]
            offset += (i[0] * WCHAR_BYTES) + WCHAR_BYTES

        if best_match is None:
            return 0

        self.state.add_constraints(*(best_match[1]))
        return best_match[0]

class libc_wcslen(angr.SimProcedure):

    max_null_index = 1024

    def run(self, s):
        null = self.state.solver.BVV(0, WCHAR_BYTES * 8)
        r, c, i = self.state.memory.find(s, null, self.max_null_index,
                max_symbolic_bytes=128, char_size=WCHAR_BYTES)
        self.state.add_constraints(*c)
        return (r - s) // WCHAR_BYTES

class libc_wcscpy(angr.SimProcedure):

    def run(self, dest, src):
        src_len = self.inline_call(libc_wcslen, src).ret_expr
        self.inline_call(angr.SIM_PROCEDURES["libc"]["memcpy"],
                dest, src, src_len * WCHAR_BYTES + WCHAR_BYTES)
        return dest

class libc_wcsncpy(angr.SimProcedure):

    def run(self, dest, src, n):
        n_val = self.state.solver.max(n)
        for offset in range(0, n_val * WCHAR_BYTES, WCHAR_BYTES):
            wchar = self.state.memory.load(src + offset, WCHAR_BYTES,
                    endness=self.state.arch.memory_endness)
            self.state.memory.store(dest + offset, wchar,
                    endness=self.state.arch.memory_endness)
            if self.state.solver.is_true(wchar == 0):
                break

        return dest

class libc_wcspbrk(angr.SimProcedure):

    def run(self, wcs, accept):
        Or = self.state.solver.Or
        And = self.state.solver.And

        wcs_len = self.inline_call(libc_wcslen, wcs)
        acc_len = self.inline_call(libc_wcslen, accept)
        best_match = None

        for idx in range(self.state.solver.max(acc_len.ret_expr)):
            acc = self.state.memory.load(accept + (WCHAR_BYTES * idx), WCHAR_BYTES)
            if self.state.solver.is_true(acc == 0):
                break
            a, c, i = self.state.memory.find(wcs, acc, wcs_len.max_null_index,
                    max_symbolic_bytes=self.state.libc.max_symbolic_strchr * WCHAR_BYTES,
                    default=0, char_size=WCHAR_BYTES)
            if best_match is None or self.state.solver.is_true(
                    self.state.solver.And(a != 0, a < best_match[0])):
                best_match = [a, c, i]

        if best_match is None:
            return 0

        self.state.add_constraints(*(best_match[1]))
        return best_match[0]

class libc_wcsrtombs(angr.SimProcedure):

    max_dest_size = 1024

    def run(self, dest, src, len, ps):
        warn_once = True

        # return value is number of multibyte characters parsed
        ret_val = 0

        # pointer at src is updated to point after last parsed character
        src_base = self.state.memory.load(src, self.state.arch.bytes,
                endness=self.state.arch.memory_endness)

        # determine max number of characters to parse
        len_val = min(self.max_dest_size, self.state.solver.max(len))

        for offset in range(len_val):
            next_wc = self.state.memory.load(src_base + (offset * WCHAR_BYTES),
                    WCHAR_BYTES, endness=self.state.arch.memory_endness)

            if self.state.solver.is_true(next_wc < 0x80):
                # most wide encodings are backwards compatible with ASCII
                ret_val += 1
                if not self.state.solver.is_true(dest == 0):
                    mbs = next_wc.get_byte(WCHAR_BYTES - 1)
                    self.state.memory.store(dest + offset, mbs)
            else:
                if warn_once:
                    log.warning("wcsrtombs is assuming source string is all ASCII")
                    warn_once = False

                ret_val += 1
                mbs = self.state.solver.BVS('wcsrtombs_%d' % offset, 8)
                self.state.memory.store(dest + offset, mbs)

            if self.state.solver.is_true(next_wc == 0):
                # reached and copied null terminator
                break

        # update src
        src_val = src_base + (ret_val * WCHAR_BYTES)
        self.state.memory.store(src, src_val, endness=self.state.arch.memory_endness)

        return ret_val

class libc_mempcpy(angr.SimProcedure):

    def run(self, dest, src, n):
        res = self.inline_call(angr.SIM_PROCEDURES["libc"]["memcpy"],
                dest, src, n)
        return dest + n

class libc_wmempcpy(angr.SimProcedure):

    def run(self, dest, src, n):
        cpy = self.inline_call(libc_mempcpy, dest, src, n * WCHAR_BYTES)
        return cpy.ret_expr

class libc_wcsncmp(angr.SimProcedure):

    def run(self, s1, s2, n):
        # don't need to actually compare because we can just let the trace
        # determine whether they matched or not for us
        return self.state.solver.BVS("wcsncmp_ret", self.state.arch.bits)


libc_hooks = {
    # Additional functions that angr doesn't provide hooks for
    "atol": libc_atol,
    "clock_gettime": libc_clock_gettime,
    "__cxa_atexit": libc___cxa_atexit,
    "exit": angr.SIM_PROCEDURES["libc"]["exit"],
    "__fprintf_chk": libc__fprintf_chk,
    "gai_strerror": libc_gai_strerror,
    "getaddrinfo": libc_getaddrinfo,
    "getenv": libc_getenv,
    # kernel and libc have the same API
    "getcwd": angr.procedures.linux_kernel.cwd.getcwd,
    "getline": libc_getline,
    "getlogin": libc_getlogin,
    "getpwnam": libc_getpwnam,
    "malloc": angr.SIM_PROCEDURES["libc"]["malloc"],
    "mbsrtowcs": libc_mbsrtowcs,
    "realpath": libc_realpath,
    # angr's version is buggy
    "unlink": libc_unlink,
    # secure_getenv and getenv work the same from a symbolic perspective
    "secure_getenv": libc_getenv,
    "snprintf": libc_snprintf,
    "__snprintf_chk": libc__snprintf_chk,
    "strncat": libc_strncat,
    "strrchr": libc_strrchr,
    "setlocale":libc_setlocale,
    "bindtextdomain": libc_bindtextdomain,
    "textdomain": libc_textdomain,
    "signal": libc_signal,
    "symlink": libc_symlink,
    "sysconf": libc_sysconf,
    "towupper": libc_towupper,
    "mmap": angr.procedures.posix.mmap.mmap,
    "swprintf": libc_swprintf,
    "vfwprintf": libc_vfwprintf,
    "wcschr": libc_wcschr,
    "wcsrchr": libc_wcsrchr,
    "wcslen": libc_wcslen,
    "wcscpy": libc_wcscpy,
    "wcsncpy": libc_wcsncpy,
    "wcspbrk": libc_wcspbrk,
    "wcsrtombs": libc_wcsrtombs,
    "mempcpy": libc_mempcpy,
    "wmempcpy": libc_wmempcpy,
    "wcsncmp": libc_wcsncmp,
}

hook_condition = ("libc\.so.*", libc_hooks)
is_main_object = False
