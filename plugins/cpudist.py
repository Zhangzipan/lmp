#!/usr/bin/python
# @lint-avoid-python-3-compatibility-imports
#
# cpudist   Summarize on- and off-CPU time per task as a histogram.
#
# USAGE: cpudist [-h] [-O] [-T] [-m] [-P] [-L] [-p PID] [interval] [count]
#
# This measures the time a task spends on or off the CPU, and shows this time
# as a histogram, optionally per-process.
#
# Copyright 2016 Sasha Goldshtein
# Licensed under the Apache License, Version 2.0 (the "License")

from __future__ import print_function
from bcc import BPF
from time import sleep, strftime
import argparse

# for influxdb
from init_db import influx_client
from db_modules import write2db
from const import DatabaseType

from datetime import datetime

examples = """examples:
    cpudist              # summarize on-CPU time as a histogram
    cpudist -O           # summarize off-CPU time as a histogram
    cpudist 1 10         # print 1 second summaries, 10 times
    cpudist -mT 1        # 1s summaries, milliseconds, and timestamps
    cpudist -P           # show each PID separately
    cpudist -p 185       # trace PID 185 only
"""
parser = argparse.ArgumentParser(
    description="Summarize on-CPU time per task as a histogram.",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-O", "--offcpu", action="store_true",
    help="measure off-CPU time")
parser.add_argument("-T", "--timestamp", action="store_true",
    help="include timestamp on output")
parser.add_argument("-m", "--milliseconds", action="store_true",
    help="millisecond histogram")
parser.add_argument("-P", "--pids", action="store_true",
    help="print a histogram per process ID")
parser.add_argument("-L", "--tids", action="store_true",
    help="print a histogram per thread ID")
parser.add_argument("-p", "--pid",
    help="trace this PID only")
parser.add_argument("count", nargs="?", default=99999999,
    help="number of outputs")
parser.add_argument("interval", nargs="?", default=1,
    help="output interval, in seconds")
parser.add_argument("--ebpf", action="store_true",
    help=argparse.SUPPRESS)
args = parser.parse_args()
countdown = int(args.count)
debug = 0

bpf_text = """#include <uapi/linux/ptrace.h>
#include <linux/sched.h>
"""

if not args.offcpu:
    bpf_text += "#define ONCPU\n"

bpf_text += """
typedef struct pid_key {
    u64 id;
    u64 slot;
} pid_key_t;

typedef struct  original_data_key {
    u32 pid;
    u32 tgid;
    u64 timestamp;
    char comm[TASK_COMM_LEN];
    u64 cpu;
} original_data_key_t;

BPF_HASH(start, u32, u64, MAX_PID);
STORAGE

static inline void store_start(u32 tgid, u32 pid, u64 ts)
{
    if (FILTER)
        return;

    start.update(&pid, &ts);
}

static inline void update_hist(struct task_struct *prev, u64 ts)
{
    u32 prev_pid = prev->pid;
    u32 prev_tgid = prev->tgid;
    if (FILTER)
        return;

    //u64 *tsp = start.lookup(&pid);
    u64 *tsp = start.lookup(&prev_pid);
    if (tsp == 0)
        return;

    if (ts < *tsp) {
        // Probably a clock issue where the recorded on-CPU event had a
        // timestamp later than the recorded off-CPU event, or vice versa.
        return;
    }
    u64 delta = ts - *tsp;
    FACTOR
    STORE
}

int sched_switch(struct pt_regs *ctx, struct task_struct *prev)
{
    u64 ts = bpf_ktime_get_ns();
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 tgid = pid_tgid >> 32, pid = pid_tgid;

    u32 prev_pid = prev->pid;
    u32 prev_tgid = prev->tgid;

#ifdef ONCPU
    //update_hist(prev_tgid, prev_pid, ts);
    update_hist(prev, ts);
#else
    store_start(prev_tgid, prev_pid, ts);
#endif

BAIL:
#ifdef ONCPU
    store_start(tgid, pid, ts);
#else
    update_hist(tgid, pid, ts);
#endif

    return 0;
}
"""

# data structure from template
class lmp_data(object):
    def __init__(self,a,b,c,d,e,f,g):
            self.time = a
            self.glob = b
            self.pid = c
            self.tgid = d
            self.comm = e
            self.cpu = f
            self.delta=g
                  
data_struct = {"measurement":'cpudist',
               "time":[],
               "tags":['glob',],
               "fields":['time','pid','tgid','comm','cpu','delta']}

if args.pid:
    bpf_text = bpf_text.replace('FILTER', 'tgid != %s' % args.pid)
else:
    bpf_text = bpf_text.replace('FILTER', '0')
if args.milliseconds:
    bpf_text = bpf_text.replace('FACTOR', 'delta /= 1000000;')
    label = "msecs"
else:
    bpf_text = bpf_text.replace('FACTOR', 'delta /= 1000;')
    label = "usecs"
if args.pids or args.tids:
    section = "pid"
    pid = "tgid"
    if args.tids:
        pid = "pid"
        section = "tid"
    bpf_text = bpf_text.replace('STORAGE',
        'BPF_HISTOGRAM(dist, pid_key_t, MAX_PID);')
    bpf_text = bpf_text.replace('STORE',
        'pid_key_t key = {.id = ' + pid + ', .slot = bpf_log2l(delta)}; ' +
        'dist.increment(key);')
else:
    section = ""
    #bpf_text = bpf_text.replace('STORAGE', 'BPF_HISTOGRAM(dist);')
    bpf_text = bpf_text.replace('STORAGE', 'BPF_HASH(dist, original_data_key_t);')
    #bpf_text = bpf_text.replace('STORE','dist.increment(bpf_log2l(delta));')
    bpf_text = bpf_text.replace('STORE',
            'original_data_key_t key; key.pid = prev_pid;' + 'key.tgid= prev_tgid;' +'bpf_probe_read_kernel_str(key.comm, sizeof(key.comm), prev->comm);'+'key.cpu=prev->cpu;'+
            'key.timestamp=ts;'+'dist.update(&key,&delta);')



if debug or args.ebpf:
    print(bpf_text)
    if args.ebpf:
        exit()

max_pid = int(open("/proc/sys/kernel/pid_max").read())

b = BPF(text=bpf_text, cflags=["-DMAX_PID=%d" % max_pid])
b.attach_kprobe(event_re="^finish_task_switch$|^finish_task_switch\.isra\.\d$",
                fn_name="sched_switch")

# print("Tracing %s-CPU time... Hit Ctrl-C to end." %
#       ("off" if args.offcpu else "on"))

exiting = 0 if args.interval else 1
dist = b.get_table("dist")
i=0
while (1):
    try:
        sleep(int(args.interval))
    except KeyboardInterrupt:
        exiting = 1

    print()
    if args.timestamp:
        print("%-8s\n" % strftime("%H:%M:%S"), end="")
    i+=1
    def pid_to_comm(pid):
        try:
            comm = open("/proc/%d/comm" % pid, "r").read()
            return "%d %s" % (pid, comm)
        except IOError:
            return str(pid)

    # dist.print_log2_hist(label, section, section_print_fn=pid_to_comm)
    j=0
    for k,v in dist.items():
        j+=1
        #print("end_run_time: %-15d pid:%-5d  tgid:%-5d comm:%-15s cpu:%-5d delta:%-5d" %(k.timestamp,k.pid,k.tgid,k.comm,k.cpu,v.value))
        # write to influxdb
        test_data = lmp_data(k.timestamp,'glob',k.pid,k.tgid,k.comm, k.cpu ,v.value)
        #print(test_data)
        write2db(data_struct, test_data, influx_client, DatabaseType.INFLUXDB.value)
        print("i=%-5d  j=%-5d "%(i,j))
    dist.clear()

    print("i=%-5d  j=%-5d "%(i,j))
    countdown -= 1
    if exiting or countdown == 0:
        exit()
