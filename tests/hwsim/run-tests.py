#!/usr/bin/env python2
#
# Test case executor
# Copyright (c) 2013-2015, Jouni Malinen <j@w1.fi>
#
# This software may be distributed under the terms of the BSD license.
# See README for more details.

import os
import re
import sys
import time
from datetime import datetime
import argparse
import subprocess
import termios

import logging
logger = logging.getLogger()

try:
    import sqlite3
    sqlite3_imported = True
except ImportError:
    sqlite3_imported = False

scriptsdir = os.path.dirname(os.path.realpath(sys.modules[__name__].__file__))
sys.path.append(os.path.join(scriptsdir, '..', '..', 'wpaspy'))

from wpasupplicant import WpaSupplicant
from hostapd import HostapdGlobal
from check_kernel import check_kernel
from wlantest import Wlantest
from utils import HwsimSkip

def set_term_echo(fd, enabled):
    [iflag, oflag, cflag, lflag, ispeed, ospeed, cc] = termios.tcgetattr(fd)
    if enabled:
        lflag |= termios.ECHO
    else:
        lflag &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSANOW,
                      [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])

def reset_devs(dev, apdev):
    ok = True
    for d in dev:
        try:
            d.reset()
        except Exception, e:
            logger.info("Failed to reset device " + d.ifname)
            print str(e)
            ok = False

    wpas = None
    try:
        wpas = WpaSupplicant(global_iface='/tmp/wpas-wlan5')
        ifaces = wpas.global_request("INTERFACES").splitlines()
        for iface in ifaces:
            if iface.startswith("wlan"):
                wpas.interface_remove(iface)
    except Exception, e:
        pass
    if wpas:
        wpas.close_ctrl()

    try:
        hapd = HostapdGlobal()
        hapd.flush()
        hapd.remove('wlan3-3')
        hapd.remove('wlan3-2')
        for ap in apdev:
            hapd.remove(ap['ifname'])
    except Exception, e:
        logger.info("Failed to remove hostapd interface")
        print str(e)
        ok = False
    return ok

def add_log_file(conn, test, run, type, path):
    if not os.path.exists(path):
        return
    contents = None
    with open(path, 'r') as f:
        contents = f.read()
    if contents is None:
        return
    sql = "INSERT INTO logs(test,run,type,contents) VALUES(?, ?, ?, ?)"
    params = (test, run, type, sqlite3.Binary(contents))
    try:
        conn.execute(sql, params)
        conn.commit()
    except Exception, e:
        print "sqlite: " + str(e)
        print "sql: %r" % (params, )

def report(conn, prefill, build, commit, run, test, result, duration, logdir,
           sql_commit=True):
    if conn:
        if not build:
            build = ''
        if not commit:
            commit = ''
        if prefill:
            conn.execute('DELETE FROM results WHERE test=? AND run=? AND result=?', (test, run, 'NOTRUN'))
        sql = "INSERT INTO results(test,result,run,time,duration,build,commitid) VALUES(?, ?, ?, ?, ?, ?, ?)"
        params = (test, result, run, time.time(), duration, build, commit)
        try:
            conn.execute(sql, params)
            if sql_commit:
                conn.commit()
        except Exception, e:
            print "sqlite: " + str(e)
            print "sql: %r" % (params, )

        if result == "FAIL":
            for log in [ "log", "log0", "log1", "log2", "log3", "log5",
                         "hostapd", "dmesg", "hwsim0", "hwsim0.pcapng" ]:
                add_log_file(conn, test, run, log,
                             logdir + "/" + test + "." + log)

class DataCollector(object):
    def __init__(self, logdir, testname, tracing, dmesg):
        self._logdir = logdir
        self._testname = testname
        self._tracing = tracing
        self._dmesg = dmesg
    def __enter__(self):
        if self._tracing:
            output = os.path.abspath(os.path.join(self._logdir, '%s.dat' % (self._testname, )))
            self._trace_cmd = subprocess.Popen(['trace-cmd', 'record', '-o', output, '-e', 'mac80211', '-e', 'cfg80211', '-e', 'printk', 'sh', '-c', 'echo STARTED ; read l'],
                                               stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE,
                                               stderr=open('/dev/null', 'w'),
                                               cwd=self._logdir)
            l = self._trace_cmd.stdout.read(7)
            while self._trace_cmd.poll() is None and not 'STARTED' in l:
                l += self._trace_cmd.stdout.read(1)
            res = self._trace_cmd.returncode
            if res:
                print "Failed calling trace-cmd: returned exit status %d" % res
                sys.exit(1)
    def __exit__(self, type, value, traceback):
        if self._tracing:
            self._trace_cmd.stdin.write('DONE\n')
            self._trace_cmd.wait()
        if self._dmesg:
            output = os.path.join(self._logdir, '%s.dmesg' % (self._testname, ))
            num = 0
            while os.path.exists(output):
                output = os.path.join(self._logdir, '%s.dmesg-%d' % (self._testname, num))
                num += 1
            subprocess.call(['dmesg', '-c'], stdout=open(output, 'w'))

def rename_log(logdir, basename, testname, dev):
    try:
        import getpass
        srcname = os.path.join(logdir, basename)
        dstname = os.path.join(logdir, testname + '.' + basename)
        num = 0
        while os.path.exists(dstname):
            dstname = os.path.join(logdir,
                                   testname + '.' + basename + '-' + str(num))
            num = num + 1
        os.rename(srcname, dstname)
        if dev:
            dev.relog()
            subprocess.call(['chown', '-f', getpass.getuser(), srcname])
    except Exception, e:
        logger.info("Failed to rename log files")
        logger.info(e)

def main():
    tests = []
    test_modules = []
    files = os.listdir(scriptsdir)
    for t in files:
        m = re.match(r'(test_.*)\.py$', t)
        if m:
            logger.debug("Import test cases from " + t)
            mod = __import__(m.group(1))
            test_modules.append(mod.__name__.replace('test_', '', 1))
            for key,val in mod.__dict__.iteritems():
                if key.startswith("test_"):
                    tests.append(val)
    test_names = list(set([t.__name__.replace('test_', '', 1) for t in tests]))

    run = None

    parser = argparse.ArgumentParser(description='hwsim test runner')
    parser.add_argument('--logdir', metavar='<directory>',
                        help='log output directory for all other options, ' +
                             'must be given if other log options are used')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-d', const=logging.DEBUG, action='store_const',
                       dest='loglevel', default=logging.INFO,
                       help="verbose debug output")
    group.add_argument('-q', const=logging.WARNING, action='store_const',
                       dest='loglevel', help="be quiet")

    parser.add_argument('-S', metavar='<sqlite3 db>', dest='database',
                        help='database to write results to')
    parser.add_argument('--prefill-tests', action='store_true', dest='prefill',
                        help='prefill test database with NOTRUN before all tests')
    parser.add_argument('--commit', metavar='<commit id>',
                        help='commit ID, only for database')
    parser.add_argument('-b', metavar='<build>', dest='build', help='build ID')
    parser.add_argument('-L', action='store_true', dest='update_tests_db',
                        help='List tests (and update descriptions in DB)')
    parser.add_argument('-T', action='store_true', dest='tracing',
                        help='collect tracing per test case (in log directory)')
    parser.add_argument('-D', action='store_true', dest='dmesg',
                        help='collect dmesg per test case (in log directory)')
    parser.add_argument('--shuffle-tests', action='store_true',
                        dest='shuffle_tests',
                        help='Shuffle test cases to randomize order')
    parser.add_argument('--split', help='split tests for parallel execution (<server number>/<total servers>)')
    parser.add_argument('--no-reset', action='store_true', dest='no_reset',
                        help='Do not reset devices at the end of the test')
    parser.add_argument('--long', action='store_true',
                        help='Include test cases that take long time')
    parser.add_argument('-f', dest='testmodules', metavar='<test module>',
                        help='execute only tests from these test modules',
                        type=str, choices=[[]] + test_modules, nargs='+')
    parser.add_argument('-l', metavar='<modules file>', dest='mfile',
                        help='test modules file name')
    parser.add_argument('-i', action='store_true', dest='stdin_ctrl',
                        help='stdin-controlled test case execution')
    parser.add_argument('tests', metavar='<test>', nargs='*', type=str,
                        help='tests to run (only valid without -f)',
                        choices=[[]] + test_names)

    args = parser.parse_args()

    if (args.tests and args.testmodules) or (args.tests and args.mfile) or (args.testmodules and args.mfile):
        print 'Invalid arguments - only one of (test, test modules, modules file) can be given.'
        sys.exit(2)

    if args.database:
        if not sqlite3_imported:
            print "No sqlite3 module found"
            sys.exit(2)
        conn = sqlite3.connect(args.database)
        conn.execute('CREATE TABLE IF NOT EXISTS results (test,result,run,time,duration,build,commitid)')
        conn.execute('CREATE TABLE IF NOT EXISTS tests (test,description)')
        conn.execute('CREATE TABLE IF NOT EXISTS logs (test,run,type,contents)')
    else:
        conn = None

    if conn:
        run = int(time.time())

    # read the modules from the modules file
    if args.mfile:
	args.testmodules = []
	with open(args.mfile) as f:
	    for line in f.readlines():
		line = line.strip()
		if not line or line.startswith('#'):
		    continue
	        args.testmodules.append(line)

    tests_to_run = []
    if args.tests:
        for selected in args.tests:
            for t in tests:
                name = t.__name__.replace('test_', '', 1)
                if name == selected:
                    tests_to_run.append(t)
    else:
        for t in tests:
            name = t.__name__.replace('test_', '', 1)
            if args.testmodules:
                if not t.__module__.replace('test_', '', 1) in args.testmodules:
                    continue
            tests_to_run.append(t)

    if args.update_tests_db:
        for t in tests_to_run:
            name = t.__name__.replace('test_', '', 1)
            if t.__doc__ is None:
                print name + " - MISSING DESCRIPTION"
            else:
                print name + " - " + t.__doc__
            if conn:
                sql = 'INSERT OR REPLACE INTO tests(test,description) VALUES (?, ?)'
                params = (name, t.__doc__)
                try:
                    conn.execute(sql, params)
                except Exception, e:
                    print "sqlite: " + str(e)
                    print "sql: %r" % (params,)
        if conn:
            conn.commit()
            conn.close()
        sys.exit(0)

    if not args.logdir:
        if os.path.exists('logs/current'):
            args.logdir = 'logs/current'
        else:
            args.logdir = 'logs'

    # Write debug level log to a file and configurable verbosity to stdout
    logger.setLevel(logging.DEBUG)

    stdout_handler = logging.StreamHandler()
    stdout_handler.setLevel(args.loglevel)
    logger.addHandler(stdout_handler)

    file_name = os.path.join(args.logdir, 'run-tests.log')
    log_handler = logging.FileHandler(file_name)
    log_handler.setLevel(logging.DEBUG)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    log_formatter = logging.Formatter(fmt)
    log_handler.setFormatter(log_formatter)
    logger.addHandler(log_handler)

    dev0 = WpaSupplicant('wlan0', '/tmp/wpas-wlan0')
    dev1 = WpaSupplicant('wlan1', '/tmp/wpas-wlan1')
    dev2 = WpaSupplicant('wlan2', '/tmp/wpas-wlan2')
    dev = [ dev0, dev1, dev2 ]
    apdev = [ ]
    apdev.append({"ifname": 'wlan3', "bssid": "02:00:00:00:03:00"})
    apdev.append({"ifname": 'wlan4', "bssid": "02:00:00:00:04:00"})

    for d in dev:
        if not d.ping():
            logger.info(d.ifname + ": No response from wpa_supplicant")
            return
        logger.info("DEV: " + d.ifname + ": " + d.p2p_dev_addr())
    for ap in apdev:
        logger.info("APDEV: " + ap['ifname'])

    passed = []
    skipped = []
    failed = []

    # make sure nothing is left over from previous runs
    # (if there were any other manual runs or we crashed)
    if not reset_devs(dev, apdev):
        if conn:
            conn.close()
            conn = None
        sys.exit(1)

    if args.dmesg:
        subprocess.call(['dmesg', '-c'], stdout=open('/dev/null', 'w'))

    if conn and args.prefill:
        for t in tests_to_run:
            name = t.__name__.replace('test_', '', 1)
            report(conn, False, args.build, args.commit, run, name, 'NOTRUN', 0,
                   args.logdir, sql_commit=False)
        conn.commit()

    if args.split:
        vals = args.split.split('/')
        split_server = int(vals[0])
        split_total = int(vals[1])
        logger.info("Parallel execution - %d/%d" % (split_server, split_total))
        split_server -= 1
        tests_to_run.sort(key=lambda t: t.__name__)
        tests_to_run = [x for i,x in enumerate(tests_to_run) if i % split_total == split_server]

    if args.shuffle_tests:
        from random import shuffle
        shuffle(tests_to_run)

    count = 0
    if args.stdin_ctrl:
        print "READY"
        sys.stdout.flush()
        num_tests = 0
    else:
        num_tests = len(tests_to_run)
    if args.stdin_ctrl:
        set_term_echo(sys.stdin.fileno(), False)
    while True:
        if args.stdin_ctrl:
            test = sys.stdin.readline()
            if not test:
                break
            test = test.splitlines()[0]
            if test == '':
                break
            t = None
            for tt in tests:
                name = tt.__name__.replace('test_', '', 1)
                if name == test:
                    t = tt
                    break
            if not t:
                print "NOT-FOUND"
                sys.stdout.flush()
                continue
        else:
            if len(tests_to_run) == 0:
                break
            t = tests_to_run.pop(0)

        name = t.__name__.replace('test_', '', 1)
        if log_handler:
            log_handler.stream.close()
            logger.removeHandler(log_handler)
            file_name = os.path.join(args.logdir, name + '.log')
            log_handler = logging.FileHandler(file_name)
            log_handler.setLevel(logging.DEBUG)
            log_handler.setFormatter(log_formatter)
            logger.addHandler(log_handler)

        reset_ok = True
        with DataCollector(args.logdir, name, args.tracing, args.dmesg):
            count = count + 1
            msg = "START {} {}/{}".format(name, count, num_tests)
            logger.info(msg)
            if args.loglevel == logging.WARNING:
                print msg
                sys.stdout.flush()
            if t.__doc__:
                logger.info("Test: " + t.__doc__)
            start = datetime.now()
            for d in dev:
                try:
                    d.dump_monitor()
                    if not d.ping():
                        raise Exception("PING failed for {}".format(d.ifname))
                    if not d.global_ping():
                        raise Exception("Global PING failed for {}".format(d.ifname))
                    d.request("NOTE TEST-START " + name)
                except Exception, e:
                    logger.info("Failed to issue TEST-START before " + name + " for " + d.ifname)
                    logger.info(e)
                    print "FAIL " + name + " - could not start test"
                    if conn:
                        conn.close()
                        conn = None
                    if args.stdin_ctrl:
                        set_term_echo(sys.stdin.fileno(), True)
                    sys.exit(1)
            try:
                if t.func_code.co_argcount > 2:
                    params = {}
                    params['logdir'] = args.logdir
                    params['long'] = args.long
                    t(dev, apdev, params)
                elif t.func_code.co_argcount > 1:
                    t(dev, apdev)
                else:
                    t(dev)
                result = "PASS"
            except HwsimSkip, e:
                logger.info("Skip test case: %s" % e)
                result = "SKIP"
            except Exception, e:
                logger.info(e)
                if args.loglevel == logging.WARNING:
                    print "Exception: " + str(e)
                result = "FAIL"
            for d in dev:
                try:
                    d.dump_monitor()
                    d.request("NOTE TEST-STOP " + name)
                except Exception, e:
                    logger.info("Failed to issue TEST-STOP after {} for {}".format(name, d.ifname))
                    logger.info(e)
                    result = "FAIL"
            wpas = None
            try:
                wpas = WpaSupplicant(global_iface="/tmp/wpas-wlan5")
                rename_log(args.logdir, 'log5', name, wpas)
                if not args.no_reset:
                    wpas.remove_ifname()
            except Exception, e:
                pass
            if wpas:
                wpas.close_ctrl()
            if args.no_reset:
                print "Leaving devices in current state"
            else:
                reset_ok = reset_devs(dev, apdev)

            for i in range(0, 3):
                rename_log(args.logdir, 'log' + str(i), name, dev[i])
            try:
                hapd = HostapdGlobal()
            except Exception, e:
                print "Failed to connect to hostapd interface"
                print str(e)
                reset_ok = False
                result = "FAIL"
                hapd = None
            rename_log(args.logdir, 'hostapd', name, hapd)
            if hapd:
                del hapd
                hapd = None

            wt = Wlantest()
            rename_log(args.logdir, 'hwsim0.pcapng', name, wt)
            rename_log(args.logdir, 'hwsim0', name, wt)
            if os.path.exists(os.path.join(args.logdir, 'fst-wpa_supplicant')):
                rename_log(args.logdir, 'fst-wpa_supplicant', name, None)
            if os.path.exists(os.path.join(args.logdir, 'fst-hostapd')):
                rename_log(args.logdir, 'fst-hostapd', name, None)

        end = datetime.now()
        diff = end - start

        if result == 'PASS' and args.dmesg:
            if not check_kernel(os.path.join(args.logdir, name + '.dmesg')):
                logger.info("Kernel issue found in dmesg - mark test failed")
                result = 'FAIL'

        if result == 'PASS':
            passed.append(name)
        elif result == 'SKIP':
            skipped.append(name)
        else:
            failed.append(name)

        report(conn, args.prefill, args.build, args.commit, run, name, result,
               diff.total_seconds(), args.logdir)
        result = "{} {} {} {}".format(result, name, diff.total_seconds(), end)
        logger.info(result)
        if args.loglevel == logging.WARNING:
            print result
            sys.stdout.flush()

        if not reset_ok:
            print "Terminating early due to device reset failure"
            break
    if args.stdin_ctrl:
        set_term_echo(sys.stdin.fileno(), True)

    if log_handler:
        log_handler.stream.close()
        logger.removeHandler(log_handler)
        file_name = os.path.join(args.logdir, 'run-tests.log')
        log_handler = logging.FileHandler(file_name)
        log_handler.setLevel(logging.DEBUG)
        log_handler.setFormatter(log_formatter)
        logger.addHandler(log_handler)

    if conn:
        conn.close()

    if len(failed):
        logger.info("passed {} test case(s)".format(len(passed)))
        logger.info("skipped {} test case(s)".format(len(skipped)))
        logger.info("failed tests: " + ' '.join(failed))
        if args.loglevel == logging.WARNING:
            print "failed tests: " + ' '.join(failed)
        sys.exit(1)
    logger.info("passed all {} test case(s)".format(len(passed)))
    if len(skipped):
        logger.info("skipped {} test case(s)".format(len(skipped)))
    if args.loglevel == logging.WARNING:
        print "passed all {} test case(s)".format(len(passed))
        if len(skipped):
            print "skipped {} test case(s)".format(len(skipped))

if __name__ == "__main__":
    main()
