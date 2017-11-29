# Copyright (C) 2015-2017 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import datetime
import dateutil.parser
import os
import logging
import re

from cuckoo.common.abstracts import BehaviorHandler

log = logging.getLogger(__name__)

class FilteredProcessLog(list):
    def __init__(self, eventstream, **kwfilters):
        self.eventstream = eventstream
        self.kwfilters = kwfilters

    def __iter__(self):
        for event in self.eventstream:
            for k, v in self.kwfilters.items():
                if event[k] != v:
                    continue

                del event["type"]
                yield event

    def __nonzero__(self):
        return True

class LinuxSystemTap(BehaviorHandler):
    """Parses systemtap generated plaintext logs (see
    stuff/systemtap/strace.stp)."""

    key = "processes"

    def __init__(self, *args, **kwargs):
        super(LinuxSystemTap, self).__init__(*args, **kwargs)

        self.processes = []
        self.forkmap = {}
        self.behavior = {}
        self.matched = False

    def handles_path(self, path):
        if path.endswith(".stap"):
            self.matched = True
            return True

    def parse(self, path):
        parser = StapParser(open(path, "rb"))

        for syscall in parser:
            # syscall specific hooks
            self.pre_hook(syscall)

            pid = syscall["pid"]

            # skip first analyzer process
            if pid not in self.forkmap:
                continue

            if self.is_newpid(pid):
                p_pid = self.forkmap.get(pid, -1)
                calls = FilteredProcessLog(parser, pid=pid)
                process = {
                    "type": "process",
                    "pid": pid,
                    "ppid": p_pid,
                    "process_name": syscall["process_name"],
                    "first_seen": syscall["time"],
                    "command_line": "",
                    "calls": calls,
                }
                self.processes.append(process)
                self.behavior[pid] = BehaviorReconstructor()
                #yield process
            
            for category, arg in self.behavior[pid].process_apicall(syscall):    
                yield {
                        "type": "generic",
                        "pid": pid,
                        "category": category,
                        "value": arg,
                    }

            p = self.post_hook(syscall)
            if not p == None:
                yield p


    def pre_hook(self, syscall):
        if syscall["api"] == "clone" or syscall["api"] == "fork":
            self.forkmap[int(syscall["return_value"])] = syscall["pid"]

    def post_hook(self, syscall):
        if syscall["api"] == "execve":
            pid = self.get_proc(syscall["pid"])
            # only update proc info after first succesful execve in this pid
            if not syscall["return_value"] and not pid["command_line"]:
                pid["process_name"] = os.path.basename(
                    str(syscall["arguments"]["p0"])
                )
                pid["command_line"] = " ".join(syscall["arguments"]["p1"])
                return pid;

    def get_proc(self, pid):
        for process in self.processes:
            if process["pid"] == pid:
                return process

    def is_newpid(self, pid):
        return not any(p["pid"] == pid for p in self.processes)

    def run(self):
        if not self.matched:
            return

        self.processes.sort(key=lambda process: process["first_seen"])
        return self.processes

def single(key, value):
    return [(key, value)]

def multiple(*l):
    return l

class BehaviorReconstructor(object):  
    """Reconstructs the behavior of behavioral API logs."""
    def __init__(self):
        self.files = {}
        self.sockets = {}


    def process_apicall(self, event):
        fn = getattr(self, "_api_%s" % event["api"], None)
        if fn is not None:
            ret = fn(
                event["return_value"], event["arguments"], event.get("status")
            )
            return ret or []
        return []

    def _api_open(self, return_value, arguments, status):
        self.files[return_value] = arguments["path"]
        return single("files_opened",(arguments["path"]))

    def _api_write(self, return_value, arguments, status):
        if arguments["fd"] in self.files :
            return single("files_writen",(self.files[arguments["fd"]]))

    def _api_read(self, return_value, arguments, status):
        if arguments["fd"] in self.files :
            return single("files_read",(self.files[arguments["fd"]]))

    def _api_close(self, return_value, arguments, status):
        if arguments["fd"] in self.files: self.files.pop(arguments["fd"], None)
        if arguments["fd"] in self.sockets: self.sockets.pop(arguments["fd"], None)

    def _api_stat(self, return_value, arguments, status):
        return single("file_exists",(arguments["path"]))

    def _api_connect(self, return_value, arguments, flags):
        return single("connects_ip", (arguments["addr"]))

    def _api_socket(self, return_value, arguments, flags):
        self.sockets[return_value] = arguments
        return single("socket", (arguments["type"]))


class StapParser(object):
    """Handle .stap logs from the Linux analyzer."""

    def __init__(self, fd):
        self.fd = fd

    def __iter__(self):
        self.fd.seek(0)

        for line in self.fd:
            # 'Thu May  7 14:58:43 2015.390178 python@7f798cb95240[2114] close(6) = 0\n'
            # datetime is 31 characters
            datetimepart, r = line[:31], line[32:]

            # incredibly sophisticated date time handling
            dtms = datetime.timedelta(0, 0, int(datetimepart.split(".", 1)[1]))
            dt = dateutil.parser.parse(datetimepart.split(".", 1)[0]) + dtms

            parts = []
            for delim in ("@", "[", "]", "(", ")", "= ", " (", ")"):
                part, _, r = r.strip().partition(delim)
                parts.append(part)

            pname, ip, pid, fn, args, _, retval, ecode = parts
            arguments = self.parse_args(args)
            pid = int(pid) if pid.isdigit() else -1

            event = {
                "time": dt, "process_name": pname, "pid": pid,
                "instruction_pointer": ip, "api": fn, "arguments": arguments,
                "return_value": retval, "status": ecode, "category" : "default",
                "type": "apicall", "raw": line,
            }
            getattr(self,"post_parse_%s" % fn, lambda event: None)(event)
            yield event

    def parse_args(self, args):
        p_args, n_args = {}, 0

        while args:
            args = args.lstrip(", ")
            delim = self.get_delim(args)
            arg, _, args = args.partition(delim)
            p_args["p%u" % n_args] = self.parse_arg(arg)
            n_args += 1

        return p_args

    def get_delim(self, argstr):
        if self.is_array(argstr):
            return "]"
        elif self.is_struct(argstr):
            return "}"
        else:
            return ", "

    def parse_arg(self, argstr):
        if self.is_array(argstr):
            return self.parse_array(argstr)
        elif self.is_struct(argstr):
            return self.parse_struct(argstr)
        elif self.is_string(argstr):
            return self.parse_string(argstr)
        else:
            return argstr

    def parse_array(self, argstr):
        return [self.parse_arg(a) for a in argstr.lstrip("[").split(", ")]

    def parse_struct(self, argstr):
        # Return as regular array if elements aren't named.
        if "=" not in argstr:
            return self.parse_array(argstr.lstrip("{"))

        # Return as dict, parse value as array and struct when appropriate.
        parsed = {}
        arg = argstr.lstrip("{")
        while arg:
            key, _, arg = arg.partition("=")
            delim = self.get_delim(arg)
            if delim != ", ":
                delim += ", "
            val, _, arg = arg.partition(delim)
            parsed[key] = self.parse_arg(val)

        return parsed

    def parse_string(self, argstr):
        return argstr.strip("\"").decode("string_escape")

    def is_array(self, arg):
        return arg.startswith("[") and not arg.startswith("[/*")

    def is_struct(self, arg):
        return arg.startswith("{")

    def is_string(self, arg):
        return arg.startswith("\"") and arg.endswith("\"")

    def rename_args(self, args, mapping):
        for key,newKey in mapping.items():
            if args.get(key):
                args[newKey] = args[key]
                del args[key]

    def post_parse_rt_sigaction(self, event):
        self.rename_args(event["arguments"],
            {"p0" : "signal", "p1" : "act", "p2" : "oldact"})

    def post_parse_default_file(self, event):
        event["category"] = "file"
        self.rename_args(event["arguments"],
            {"p0" : "path", "p1" : "mode", "p2" : "flags"})
    post_parse_open = post_parse_default_file
    post_parse_creat = post_parse_default_file
    post_parse_chmode = post_parse_default_file
    post_parse_chdir = post_parse_default_file
    post_parse_stat = post_parse_default_file

    def post_parse_chown(self, event):
        event["category"] = "file"
        self.rename_args(event["arguments"],
            {"p0" : "path", "p1" : "owner", "p2" : "group"})

    def post_parse_write(self, event):
        event["category"] = "file"
        self.rename_args(event["arguments"],
            {"p0" : "fd", "p1" : "buffer", "p2" : "count"})
    post_parse_read = post_parse_write
    post_parse_close = post_parse_write

    def post_parse_socket(self, event):
        event["category"] = "network"
        self.rename_args(event["arguments"],
            {"p0" : "domain", "p1" : "type", "p2" : "protocol"})

    def post_parse_getsockopt(self, event):
        event["category"] = "network"
        self.rename_args(event["arguments"],
            {"p0" : "sockfd", "p1" : "level", "p2" : "optname", "p3" : "optval", "p3" : "optlen"})
    post_parse_setsockopt = post_parse_getsockopt

    def post_parse_accept(self, event):
        event["category"] = "network"
        self.rename_args(event["arguments"],
            {"p0" : "sockfd", "p1" : "addr", "p2" : "addrlen", "p3" : "flags"})
    post_parse_accept4 = post_parse_accept
    post_parse_connect = post_parse_accept
    post_parse_bind = post_parse_accept
    post_parse_getsockname = post_parse_accept
    post_parse_getpeername = post_parse_accept
