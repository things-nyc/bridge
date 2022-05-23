##############################################################################
#
# Name: rn2903.py
#
# Function:
#       Rn2903() class
#
# Copyright notice and license:
#       See LICENSE.md
#
# Author:
#       Terry Moore
#
##############################################################################

### system imports ###
import binascii
from enum import Enum,unique
import logging
import queue
import serial
import sys
import threading
import time

##############################################################################
#
# Class for the RN2903. Starts a thread which then accepts commaands on
# a queue; and another thread that gets responses.
#
# There are several kinds of RN2903 commands:
#
# 1. commands that have immediate response of some kind of status
# 2. commands that have immediate response of data
# 3. commands that respond immediately, then (if "ok"), send a second response.
#       mac tx
#       mac join
#       radio rx
#       radio tx
# 4. commands with no response at all
#       sys eraseFW
#
#
##############################################################################

class Rn2903():
    def __init__(self, port_name, *, baudrate=57600, cmd_timeout_sec=0.1, log=None):
        self.READ_TIMEOUT = 0
        self.CMD_TIMEOUT = cmd_timeout_sec

        if log == None:
            log = logging

        self.log = log

        try:
            self.radio = serial.Serial(
                port=port_name,
                baudrate=baudrate,
                timeout=self.READ_TIMEOUT,
                inter_byte_timeout=None,
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
    def _getchars(self):
        """
        Read as many characters as possible from the radio

        We read one character unconditionally; this will cause us
        to block if there are no characters waiting. We depend on
        the read-timeout to get us out, so there's a maximum
        latency here.
        """
        data = self.radio.read(1)
        while True:
            in_waiting = self.radio.in_waiting
            if in_waiting > 0:
                data += self.radio.read(in_waiting)
            else:
                break
        return data

    # iterator that returns lines from the radio
    def _nextline(self, exitEvent):
        data = b''
        while not exitEvent.is_set():
            buf = b''
            try:
                buf = self._getchars()
            except IOError:
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

#    def _sleep(self, secs):
#        def done(e):
#            e.set()
#        e = threading.Event()
#        t = threading.Timer(0.1, done, args=(e,))
#        t.start()
#        e.wait()

    def _cmdworker_inner(self):
        # set state to stIdle:
        cmd = None

        # set up line iterator
        next_line_from_modem = self._nextline(self._exit)

        # process lines until we run out
        try:
            for line in next_line_from_modem:
                self.log.debug("received line: %s", line)
                if line == None and cmd == None:
                    # try to fetch and launch next command,
                    cmd = self._cmdworker_promote()
                elif line != None:
                    # got a response of some kind.
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
        finally:
            if cmd != None:
                cmd.set_complete(self.Result.STATUS_INTERNAL_ERROR)
        pass

    def _cmdworker_promote(self):
        cmd = None
        try:
            cmd = self._cmdqueue.get(block=True, timeout=self.CMD_TIMEOUT)
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
        def __init__(self, port, message):
            self.port = port
            self.message = message

    def _process_downlink(self, port, message):
        self._rxqueue.put(self.Message(port, message), block=False)

    ##########################################################################
    #
    #   APIs for use by the client, in ascending order of abstraction
    #
    ##########################################################################
    def send_command(self, words):
        """ send command """
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
        self.log.debug("queue uplink command: %s", str(words.join(b' '), encoding='ascii'))
        self.send_command_indication()

