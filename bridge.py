# quick hack to bridge characters from class C lora modem to random local eqiupment

# input:
#   com port for the local equipment
#   baud rate for local equipment
#   com port for the LoRaWAN modem
#   baud rate for the lorawan modem
#
# These come from the command line:
#   --local {comport}
#   --lbaud {baud} for local com port, default is 115200
#   --remote {comport}
#   --rbaud {baud} for remote mode, default is 57600
#
# Defaults are tweaked for our initial test setup
#
# After scanning the command line arguments, two activities are created.
# One activity moves characters from local to remote asynchronously; the other
# moves characters from remote to local.
#
# The data rates may be different, so we have to worry about buffering.
# The first level of buffering is in the system com port driver, which
# typically buffer kilobytes of data. If needed, a second level of buffering
# will be done here.
#
# This goes on forver.
#

import argparse
import binascii
from enum import Enum, unique
import logging
import pty
import os
import queue
import serial
import re
import subprocess
import sys
import threading
import time

##############################################################################
#
# The argument parser
#
##############################################################################

def ParseArguments():
    parser = argparse.ArgumentParser(description="Bridge LoRaWAN class C modem to local comm port")
    parser.add_argument("--local", "-l", required=True, help="Local comm port, or PTY for a shell", metavar="{comm}")
    parser.add_argument("--lbaud", default=115200, type=int, help="Baud rate for local port", metavar="{baudrate}")
    parser.add_argument("--radio", "-r", required=True, help="Radio comm port, or TEST for a local test", metavar="{comm}")
    parser.add_argument("--rbaud", default=57600, type=int, help="Baud rate for LoRaWAN modem port", metavar="{baudrate}")
    parser.add_argument("--verbose", "-v", action='count', default=0)
    parser.add_argument("--test2903", action="store_true", help="Invoke the RN2903 test instead of starting")
    return parser.parse_args()

##############################################################################
#
# Spawn a captive shell and return a file handle.
#
##############################################################################

def spawn_captive_shell(log):
    """spawn captive shell and return file handle"""
    argv = [ "/bin/bash", "--login" ]
    (childpid, parentfd) = pty.fork()
    if childpid == 0:
        # we're the child
        try:
            log.info("Spawning %s...", argv[0])
            os.execvp(argv[0], argv)
        except OSError as err:
            log.critical(f"Cannot execute %s: %s", argv[0], str(err))
            sys.exit(1)
    return (childpid, parentfd)

##############################################################################
#
# A simple class that implements a non-blocking API for pty subprocess I/O
#
##############################################################################

class PtyFile():
    """ a simple class to provide compatible non-blocking I/O to the subprocess handle when using a captive shell """

    def __init__(self, fd):
        """ constructor: cache fd, and set it non-blocking """
        self.READSIZE = 128

        self._fd = fd
        os.set_blocking(fd, False)
    
    def read(self, n):
        """ equivalent of serial.Serial::read() with timeout=0 """
        try:
            return os.read(self._fd, n)
        except BlockingIOError as err:
            return b''

    def write(self, buf):
        """ equivalent of serial.Serial::write """
        os.write(self._fd, buf)
    
    def reset_input_buffer(self):
        """ equivalent of serial.Serial::reset_input_buffer() """
        while self.read(self.READSIZE) != b"":
            # discard any available characters.
            pass

    def __del__(self):
        """ desctructor: make sure we close the fd """
        self.close()

    def close(self):
        """
        Explicitly close the fd, and invalidate the local copy.

        Exceptions are suppressed
        """
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except:
                pass
            self._fd = -1

##############################################################################
#
# Class for the RN2903. Starts a thread which then accepts commaands on
# a queue; and another thread that gets responses.
#
##############################################################################

class Rn2903():
    def __init__(self, port_name, *, baudrate=57600, read_timeout_sec=0, read_byte_timeout_sec=None, log=None):
        self.READ_TIMEOUT = read_timeout_sec
        self.READ_BYTE_TIMEOUT = read_byte_timeout_sec

        if log == None:
            log = logging

        self.log = log

        try:
            self.radio = serial.Serial(
                port=port_name,
                baudrate=baudrate,
                timeout=self.READ_TIMEOUT,
                inter_byte_timeout=self.READ_BYTE_TIMEOUT,
                exclusive=True
                )
        except Exception as err:
            self.log.error("Failed to open local port: %s", err)
            raise

        self.radio.reset_input_buffer()

        # create queues for input and output
        self._cmdqueue = queue.Queue()
        self._cmd_launch_time = -1.0
        self._rxqueue = queue.Queue()
        self._exit = threading.Event()
        self._exited = threading.Event()
    
        # create worker threads for input and output
        self.log.info("creating command worker thread")
        self._cmdthread = threading.Thread(target=self._cmdworker, name="rn2903.cmd", daemon=True)
        self.log.info("starting command worker thread")
        self._cmdthread.start()

    def request_exit(self):
        self.log.debug("requesting RN2903 exit")
        self._exit.set()
        self._exited.wait()
        self.log.debug("RN2903 exited")

    @unique
    class Result(Enum):
        STATUS_PENDING = 0

        # these correspond to the modem responses
        STATUS_OK = 1
        STATUS_BUSY = 2
        STATUS_FRAM_COUNTER_ERR_REJOIN_NEEDED = 3
        STATUS_INVALID_CLASS = 4
        STATUS_INVALID_DATA_LEN = 5
        STATUS_INVALID_PARAM = 6
        STATUS_KEYS_NOT_INIT = 7
        STATUS_MAC_PAUSED = 8
        STATUS_MULTICAST_KEYS_NOT_SET = 9
        STATUS_NO_FREE_CH = 10
        STATUS_NOT_JOINED = 11
        STATUS_SILENT = 12
        STATUS_ERR = 13
        STATUS_UNMATCHED_RESPONSE = 14

        # this means we got a response (not a status)
        STATUS_RESPONSE_RECEIVED = 98

        # this means internal error
        STATUS_INTERNAL_ERROR = 99

        def is_complete(self):
            return self.value >= self.STATUS_OK.value
        def has_response(self):
            return self == self.STATUS_RESPONSE_RECEIVED

    class RadioError(Exception):
        pass

    class Command():
        def __init__(self, *, words=None, event = None):
            self.status = Rn2903.Result.STATUS_PENDING
            self.result_words = None
            self.words = words

            self.event = event
            if event != None:
                event.clear()
        
        def set_status(self, status):
            self.status = status
        
        def set_response(self, words):
            self.result_words = words

        def set_complete(self):
            if self.status == Rn2903.Result.STATUS_PENDING:
                self.set_status(Rn2903.Result.STATUS_INTERNAL_ERROR)
            if self.event != None:
                self.event.set()

    def send_command(self, words):
        event = threading.Event()
        cmd = self.Command(words=words, event=event)
        self._cmdqueue.put(cmd)
        event.wait()
        return (cmd.status, cmd.result_words)

    def mac_send_command_get_response(self, buf):
        (s, w) = self.send_command(buf.split(b' '))
        if s.has_response():
            return str(b' '.join(w), encoding="ascii")
        raise self.RadioError(s.name())

    def mac_get_class(self):
        r = self.mac_send_command_get_response(b"mac get class")
        self.log.debug("current mac class is %s", r)
        return r

    def mac_get_channel_status(self, iChannel):
        r = self.mac_send_command_get_response(b"mac get ch status %d" % iChannel)
        match (r,):
            case ["on"]:
                v = True
            case ["off"]:
                v = False
            case _:
                raise self.RadioError("unrecognized channel status %s" % r)

        self.log.debug("current mac channel %d status is %s", iChannel, v)
        return v

    def mac_get_channel_mask(self):
        mask = int(0)
        for i in range(72):
            v = self.mac_get_channel_status(i)
            if v:
                mask |= int(1) << i
        self.log.debug("mac channel mask: %s", binascii.hexlify(mask.to_bytes(9, byteorder='big'), '.', 2))
        return mask

    def send_command_indication(self, words):
        cmd = self.Command(words=words, event=None)
        self._cmdqueue.put(cmd)

    def write(self, port, buf):
        words = [ 'mac', 'tx', 'uncnf', b"%d" % port, binascii.hexlify(buf) ]
        (s, w) = self.send_command_indication()

    #
    # The main function of the worker thread for dealing with the modem.
    # The thread implicitly implements a FSM with two states: idle,
    # and command pending.
    #
    # In the idle state, any responses from the
    # modem are *unsolicited*; anything other than `mac_rx` gets logged
    # and discarded.
    #
    # If a command can be pulled from the tx queue, the FSM moves to the
    # command state. The command is formatted and transmitted. Received
    # responses are then parsed using the solicited command parser; error
    # codes are understood, `mac_rx` is honored, and any response other
    # than a known response is treated as the result of the command and
    # attached as a response; the command is then completed.
    #
    # To avoid deadlocks, when a command is started, we set a timer. When
    # the timer expires, we return to the idle state and complete the
    # command with STATUS_TIMED_OUT.
    #
    def _nextline(self, exitEvent):
        data = b''
        while not exitEvent.is_set():
            buf = b''
            try:
                buf = self.radio.read(256)
            except IOErr:
                break

            # if we got some data
            if len(buf) > 0:
                # append to buffer
                data += buf

                # now see whether we now have one or more complete lines
                lines = data.split(b'\r\n')

                # if the last line was not complete, push it back
                if lines[len(lines) - 1] != b'':    # last line is not terminated
                    data = lines.pop()              # -> move fragment back to queue
                else:
                    data = b''
                
                # return the complete lines, one by one
                for line in lines:
                    if line != b'':
                        yield line
            else:
                yield None

    def _cmdworker(self):
        self.log.debug("entered worker thread")
        try:
            self._cmdworker_inner()
        except:
            self.log.exception("uncaught exception in _cmdworker_inner")

        try:
            self.radio.close()
        except:
            self.log.exception("error closing radio")
        self.log.debug("RN2903._cmdworker exiting")     
        self._exited.set()

    def _sleep(self, secs):
        def done(e):
            e.set()
        e = threading.Event()
        t = threading.Timer(0.1, done, args=(e,))
        t.start()
        e.wait()

    def _cmdworker_inner(self):
        # set state to stIdle:
        cmd = None

        # set up line iterator
        next_line_from_modem = self._nextline(self._exit)

        # process lines until we run out
        for line in next_line_from_modem:
            self.log.debug("received line: %s", line)
            if line == None:
                if cmd == None:
                    cmd = self._cmdworker_promote()
                    if cmd != None:
                        continue

                # no data, sleep
                #self._sleep(0.01)
                continue

            # make words using a single blank.
            words = line.split(b' ')

            if cmd != None:
                # try to complete the command
                if self._cmdworker_process_solicited(cmd, words):
                    self.log.debug("completing command %s status %s", cmd, cmd.status)
                    cmd.set_complete()
                    cmd = None
            else:
                if not self._cmdworker_process_unsolicited(words):
                    self.log.error("unsolicited message not recognized: %s", words)
        pass

    def _cmdworker_promote(self):
        cmd = None
        try:
            cmd = self._cmdqueue.get(block=False)
        except queue.Empty:
            pass

        if cmd != None:
            self.radio.write(b' '.join(cmd.words) + b"\r\n")
            self._cmd_launch_time = time.monotonic()
            self.log.debug("sent command: %s", str(b' '.join(cmd.words), encoding="ascii"))
        return cmd

    def _cmdworker_process_solicited(self, cmd, words):
        status = None
        match words:
            case ["ok"]:
                status = self.Result.STATUS_OK

            case ["busy"]:
                status = self.Result.STATUS_BUSY

            case ["fram_counter_err_rejoin_needed"]:
                status = self.Result.STATUS_FRAM_COUNTER_ERR_REJOIN_NEEDED

            case ["invalid_class"]:
                status = self.Result.STATUS_INVALID_CLASS

            case ["invalid_data_len"]:
                status = self.Result.STATUS_INVALID_DATA_LEN

            case ["invalid_param"]:
                status = self.Result.STATUS_INVALID_PARAM

            case ["keys_not_init"]:
                status = self.Result.STATUS_KEYS_NOT_INIT

            case ["mac_paused"]:
                status = self.Result.STATUS_MAC_PAUSED

            case ["multicast_keys_not_set"]:
                status = self.Result.STATUS_MULTICAST_KEYS_NOT_SET

            case ["no_free_ch"]:
                status = self.Result.STATUS_NO_FREE_CH

            case ["not_joined"]:
                status = self.Result.STATUS_NOT_JOINED

            case ["silent"]:
                status = self.Result.STATUS_SILENT

            case ["err"]:
                status = self.Result.STATUS_ERR

            case _:
                if not self._cmdworker_process_unsolicited(words):
                    # the words are the command response
                    status = self.Result.STATUS_RESPONSE_RECEIVED
                    cmd.set_response(words)
        
        # now status is either a value or None (if the command status is not to be set)
        if status != None:
            cmd.set_status(status)
            return True
        else:
            return False

    def _cmdworker_process_unsolicited(self, words):
        match words:
            case ["mac_rx", port, message]:
                self._process_downlink(binascii.unhexlify(port)[0], binascii.unhexlify(message))
                return True

            case _:
                return False

    def _complete_command(self, status):
        # grab the singleton from the pending command queue
        cmd = None
        try:
            cmd = self._pendingqueue.get_nowait()
        except queue.Empty():
            pass
        except:
            raise

        if cmd != None:
            cmd.set_status(status)
            self._cmddonequeue.put(cmd)
        else:
            self.log.debug("_complete_command, but no pending command found")

    class Message:
        def __init__(port, message):
            self.port = port
            self.message = message

    def _process_downlink(self, port, message):
        self._rxqueue.put(Message(port, message), block=False)


##############################################################################
#
# A simple class that implements a non-blocking API for pty subprocess I/O
#
##############################################################################

class App():
    def __init__(self, args):
        self.args = args

        # initialize logging
        loglevel = logging.ERROR - 10 * args.verbose
        if loglevel < 0:
            loglevel = 0

        logging.basicConfig(level=loglevel, format='%(relativeCreated)6d %(threadName)s %(message)s')
        self.log = logging
        self.log.debug("Initializing App")

        if args.test2903:
            self.radio = Rn2903(self.args.radio, baudrate=self.args.rbaud, log=self.log)
            self.initialize_radio()
            self.log.info("Radio is initialized")
            self.radio.request_exit()
            sys.exit(0)

        # init a connection to the local port.
        try:
            if self.args.local == "PTY":
                # create a process and get handles
                (_, fdForParentUse) = spawn_captive_shell(self.log)
                self.local = PtyFile(fdForParentUse)
                self._pty = True
            else:
                self.local = serial.Serial(
                    port=self.args.local,
                    baudrate=self.args.lbaud,
                    timeout=0
                    )
                self._pty = False
        except Exception as err:
            self.log.exception("Failed to open local port: %s", err)
            sys.exit(1)

        # open the radio port
        if self.args.radio == "TEST":
            # just use the console and don't wrap or unwrap
            self._test = True
            self.remote = serial.Serial(
                port=os.ttyname(sys.stdin.fileno()),
                timeout=0
                )
        else:
            self._test = False
            self.remote = serial.Serial(
                port=self.args.radio,
                baudrate=self.args.rbaud,
                timeout=0
                )

        # get rid of anything that might be waiting in the buffers
        self.local.reset_input_buffer()
        self.remote.reset_input_buffer()

        self.data = b''
        self.downlinkPattern = re.compile(b'^mac_rx ([0-9]+) ([0-9A-F]*)$')
        return

    def initialize_radio(self):
        need_set = False

        macclass = self.radio.mac_get_class()
        if macclass != "C":
            self.log.info("mac class is not C: %s", macclass)
            need_set = True

        desired_mask = (int(1) << 65) | (int(0xFF) << 8)
        mask = self.radio.mac_get_channel_mask()
        if mask != desired_mask:
            self.log.info("mac class mask %x is not %x", mask, desired_mask)
            need_set = True

    def SaveData(self, buf):
        self.data += buf
        None

    def LookForDownlink(self):
        if self._test:
            self.local.write(self.data)
            self.data = b''
            return

        lines = self.data.split(b'\r\n')
        if lines[len(lines) - 1] != b'':
            self.data = lines.pop()
        else:
            self.data = b''
        for thisLine in lines:
            result = self.downlinkPattern.match(thisLine)
            if result != None:
                # matched!
                thisMessage = result.group(2)
                self.log.info("downlink: %s", thisMessage)
                self.local.write(binascii.unhexlify(thisMessage))
            else:
                self.log.warning("ignored: %s", thisLine.decode('utf-8', 'backslashreplace'))

    def PushRemoteToLocal(self):
        buf = self.remote.read(256)
        if len(buf) > 0:
            self.SaveData(buf)
            self.LookForDownlink()
            #self.local.write(buf)
        None

    def PushLocalToRemote(self):
        buf = self.local.read(128)
        if len(buf) > 0:
            if self._test:
                self.remote.write(buf)
            else:
                self.log.info("uplink: %s", binascii.hexlify(buf))
                self.remote.write(BuildTxMessage(1, buf))
        None

def BuildTxMessage(port, buf):
    result = b"mac tx uncnf 1 " + binascii.hexlify(buf) + b"\r\n"
    return result

def main():
    global gApp
    args = ParseArguments()
    gApp = App(args)

    while True:
        try:
            gApp.PushRemoteToLocal()
            gApp.PushLocalToRemote()
        except:
            gApp.local.close()
            gApp.remote.close()
            if gApp._test:
                subprocess.run(["stty", "sane"])
            raise
    return 0

if __name__ == '__main__':
    sys.exit(main())
