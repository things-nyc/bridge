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
# 1. commands that have immediate response: some kind of status or some kind of data
# 2. commands that respond immediately, then (if "ok"), send a second response.
#       mac tx
#       mac join
#       radio rx
#       radio tx
# 3. commands with no response at all
#       sys eraseFW -- we don't handle this in this driver at all.
#
##############################################################################

class Rn2903():
    def __init__(self, port_name, *, baudrate=57600, cmd_timeout_sec=0.1, log=None, loglevel=None):
        self.READ_TIMEOUT = 0
        self.CMD_TIMEOUT = cmd_timeout_sec
        self.PHASE_ONE_TIMEOUT = 100 * 1000 * 1000          # 100 ms
        self.PHASE_TWO_TIMEOUT = 10 * 1000 * 1000 * 1000    # 10 seconds

        if log == None:
            log = logging.getLogger(__name__)

        self.log = log
        if loglevel != None:
            self.log.setLevel(loglevel)

        self.log.info("initialize RN2903 driver")

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

        # terminate any garbage that's waiting
        self.radio.reset_input_buffer()

        # create queues for input and output
        self._cmdqueue = queue.Queue()
        self._cmd_launch_time = -1.0
        self._rxqueue = queue.Queue()
        self._started = threading.Event()
        self._exit = threading.Event()
        self._exited = threading.Event()
    
        # create worker threads for input and output
        self.log.debug("creating command worker thread")
        self._cmdthread = threading.Thread(target=self._cmdworker, name="rn2903.cmd", daemon=True)
        self.log.info("starting command worker thread")
        self._cmdthread.start()
        self._started.set()

        # and finally: launch a command that is just discarded to get the version (ignoring errors)
        for i in range(3):
            try:
                v = self.macll_send_command_get_response(b"sys get ver")
                self.sw_version = v
                self.log.info("Found radio. Version: %s", v)
                break
            except self.RadioError:
                self.log.info("'sys get ver' failed, try %d", i+1)
        else:
            self.log.error("didn't find radio, shut down")
            self.request_exit()
            self.radio.close()
            raise self.RadioError("radio not found")

    def request_exit(self):
        """ ask the radio driver to exit, and wait for it to do so """
        self.log.info("requesting radio driver exit")
        self._exit.set()
        self._exited.wait()
        self.log.debug("radio driver exited")

    def is_running(self):
        """ return true if the radio driver is running """
        return self._started.is_set() and not self._exited.is_set()

    def wait_for_exit(self):
        """ wait for radio driver to exit on its own """
        if self.is_running():
            self._exited.wait()

    @unique
    class Result(Enum):
        """ status codes for radio commands """
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
        STATUS_MAC_ERR = 15
        STATUS_RADIO_ERR = 16

        # these correspond to other failures
        STATUS_TIMEOUT = 97

        # this means we got a response (not a status)
        STATUS_RESPONSE_RECEIVED = 98

        # this means internal error
        STATUS_INTERNAL_ERROR = 99

        def is_complete(self):
            """ return True if Result indicates a completed request """
            return self.value >= self.STATUS_OK.value
        def has_response(self):
            """ return True if Result indicates that a response was received """
            return self == self.STATUS_RESPONSE_RECEIVED

    class RadioError(Exception):
        """ this is the Exception thrown for radio errors """
        pass

    class MacStatus():
        """ structured type for RN2903 mac status results words """
        def __init__(self, mask):
            """ constructor: initializes status from a bitmask """
            self.value = int(mask)
        
        def state(self):
            """ return the mac state """
            return self.value & 0xF

        def is_joined(self):
            """ return True if the mac indicates that it's joined """
            return (self.value & (1 << 4)) != 0

        def need_join(self):
            """ return True if the mac indicates that a join is needed """
            return ((self.value & (1 << 4)) == 0) or (self.value & (1 << 17) != 0)

        def is_silent(self):
            """ return True if the mac is in forced-silent mode """
            return (self.value & (1 << 7)) != 0
        
        def is_paused(self):
            """ return True if the mac has been paused """
            return (self.value & (1 << 8)) != 0

    class Command():
        """ the mac command request block """
        def __init__(self, *, words=None, event = None):
            self.status = Rn2903.Result.STATUS_PENDING
            self.result_words = None
            if words:
                self.set_cmd(words)
            self.event = event
            if event != None:
                event.clear()

        def set_cmd(self, words):
            """ set the mac command to be transmitted """
            self.words = words
            self.is_mac_tx = False
            self.is_mac_join = False
            self.is_radio_tx = False
            self.is_radio_rx = False
            match words:
                case [b"mac", b"tx", *args]:
                    self.is_mac_tx = True
                case [b"mac", b"join", *args]:
                    self.is_mac_join = True
                case [b"radio", b"tx", *args]:
                    self.is_radio_tx = True
                case [b"radio", b"rx", *args]:
                    self.is_radio_rx = True
                case _:
                    pass
            # logging.debug("is_mac_tx=%d is_mac_join=%d is_radio_tx=%d is_radio_rx=%d cmd=%s",
            #                 self.is_mac_tx, self.is_mac_join, self.is_radio_tx, self.is_radio_rx,
            #                 words)

        def set_status(self, status):
            """ set the status of the command """
            self.status = status
        
        def set_response(self, words):
            """ set the response field of the command """
            self.result_words = words

        def set_complete(self):
            """ complete the request """
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
        """
        iterator for delivering lines from the radio to the radio driver

        exitEvent: a threading.Event(); if is_set() returns true, we stop the
            iterator.

        returns a single line of text each time, without \r\n.  Blank lines
        are neer returned.  If there's no line but there might be more in
        in the futuer, returns None.  IOErrors cause iterator to terminate.
        """
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
        """ the thread worker routine for the driver """
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
        """ loop for driver thread
        
        This is a function to make code easier to read, as the caller
        wraps this in a try block to ensure that the radio gets closed.
        """
        # set state to idle.
        cmd = None

        # set up line iterator
        next_line_from_modem = self._nextline(self._exit)

        # Process lines until we run out
        # Use an exception block to make sure we complete a command if
        # we die unexpectedly.
        try:
            for line in next_line_from_modem:
                if line == None and cmd == None:
                    # try to fetch and launch next command,
                    cmd = self._cmdworker_promote()
                elif line == None and cmd != None:
                    # we are working on a command but nothing has
                    # happened. Check for timeout.
                    dt = time.monotonic_ns() - self._cmd_launch_time
                    if (cmd.phase == 1 and dt > self.PHASE_ONE_TIMEOUT) or  \
                       (cmd.phase == 2 and dt > self.PHASE_TWO_TIMEOUT)     :
                        cmd.set_status(self.Result.STATUS_TIMEOUT)
                        cmd.set_complete()
                        cmd = None
                elif line != None:
                    # got a response of some kind.
                    # make words using a single blank.
                    self.log.debug("received line: %s", line)
                    words = line.split(b' ')

                    if cmd != None:
                        # try to complete the command
                        # note that a two-phase command will advance on OK status to second phase
                        if self._cmdworker_process_solicited(cmd, words):
                            self.log.debug("completing command %s status %s", cmd, cmd.status)
                            cmd.set_complete()
                            cmd = None
                    else:
                        if not self._cmdworker_process_unsolicited(words):
                            self.log.error("unsolicited message not recognized: %s", words)
        finally:
            if cmd != None:
                cmd.set_status(self.Result.STATUS_INTERNAL_ERROR)
                cmd.set_complete()
        pass

    def _cmdworker_promote(self):
        """
        Promote next command if possible, waiting for a little while.

        This function is called when there's no radio inbound traffic and there's
        no current command. We look for a command, blocking for a maximum of
        CMD_TIMEOUT seconds.

        If a command is found, we write it to the uart, record the launch time,
        and return the command to be held by the work loop. Otherwise we return
        None.
        """
        cmd = None
        try:
            cmd = self._cmdqueue.get(block=True, timeout=self.CMD_TIMEOUT)
        except queue.Empty:
            pass

        if cmd != None:
            self.radio.write(b' '.join(cmd.words) + b"\r\n")
            self._cmd_launch_time = time.monotonic_ns()
            cmd.phase = 1
            self.log.debug("sent command: %s", str(b' '.join(cmd.words), encoding="ascii"))
        return cmd

    def _cmdworker_process_solicited(self, cmd, words):
        """ process a response received while we're really  """
        status = None
        phase = cmd.phase
        mac_tx = cmd.is_mac_tx
        mac_join = cmd.is_mac_join
        radio_tx = cmd.is_radio_tx
        radio_rx = cmd.is_radio_rx

        match words:
            case [b"ok"] if phase == 1:
                status = self.Result.STATUS_OK

            case [b"busy"] if phase == 1:
                status = self.Result.STATUS_BUSY

            case [b"fram_counter_err_rejoin_needed"] if phase == 1:
                status = self.Result.STATUS_FRAM_COUNTER_ERR_REJOIN_NEEDED

            case [b"invalid_class"] if phase == 1:
                status = self.Result.STATUS_INVALID_CLASS

            case [b"invalid_data_len"] if phase == 1:
                status = self.Result.STATUS_INVALID_DATA_LEN

            case [b"invalid_param"] if phase == 1:
                status = self.Result.STATUS_INVALID_PARAM

            case [b"keys_not_init"] if phase == 1:
                status = self.Result.STATUS_KEYS_NOT_INIT

            case [b"mac_paused"] if phase == 1:
                status = self.Result.STATUS_MAC_PAUSED

            case [b"multicast_keys_not_set"] if phase == 1:
                status = self.Result.STATUS_MULTICAST_KEYS_NOT_SET

            case [b"not_joined"] if phase == 1 and mac_tx:
                status = self.Result.STATUS_NOT_JOINED

            case [b"silent"] if phase == 1 and (mac_tx or mac_join):
                status = self.Result.STATUS_SILENT

            case [b"err"] if phase == 1:
                status = self.Result.STATUS_ERR

            case [b"mac_tx_ok"] if phase == 2:
                status = self.Result.STATUS_OK
    
            case [b"mac_rx", port, data] if phase == 2:
                self._process_downlink(port, data)
                status = self.Result.STATUS_OK

            case [b"mac_err"] if phase == 2 and mac_tx:
                status = self.Result.STATUS_MAC_ERR

            case [b"keys_not_init"] if phase == 1 and mac_join:
                status = self.Result.STATUS_KEYS_NOT_INIT

            case [b"no_free_ch"] if phase == 1 and (mac_join or mac_tx):
                status = self.Result.STATUS_NO_FREE_CH
        
            case [b"denied"] if phase == 2 and mac_join:
                status = self.Result.STATUS_JOIN_FAILED

            case [b"accepted"] if phase == 2 and mac_join:
                status = self.Result.STATUS_OK

            case [b"radio_tx_ok"] if phase == 2 and radio_tx:
                status = self.Result.STATUS_OK

            case [b"radio_err"] if phase == 2 and radio_tx or radio_rx:
                status = self.Result.STATUS_RADIO_ERR

            case [b"radio_rx", data] if phase == 2 and radio_rx:
                self._process_downlink(0, data)
                status = self.Result.STATUS_OK

            case _:
                if not self._cmdworker_process_unsolicited(words):
                    # the words are the command response
                    status = self.Result.STATUS_RESPONSE_RECEIVED
                    cmd.set_response(words)
        
        # now status is either a value or None (if the command status is not to be set)
        if phase == 1 and (mac_tx or mac_join or radio_tx or radio_rx) and status == self.Result.STATUS_OK:
            # this is a 2-phase command
            cmd.phase += 1
            return False

        if status != None:
            cmd.set_status(status)
            return True
        else:
            return False

    def _cmdworker_process_unsolicited(self, words):
        match words:
            case [b"mac_rx", port, message]:
                self._cmdworker_mac_rx(port, message)
                return True

            case _:
                return False

    def _cmdworker_mac_rx(self, port, message):
        self._process_downlink(binascii.unhexlify(port)[0], binascii.unhexlify(message))

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
    def macll_send_command(self, words):
        """ send command and wait for completion """
        event = threading.Event()
        cmd = self.Command(words=words, event=event)
        self._cmdqueue.put(cmd)
        event.wait()
        return (cmd.status, cmd.result_words)

    def macll_send_command_get_response(self, buf):
        """ send command, wait for response, and return response as a string """ 
        (s, w) = self.macll_send_command(buf.split(b' '))
        if s.has_response():
            return str(b' '.join(w), encoding="ascii")
        raise self.RadioError(s.name)

    def macll_send_command_indication(self, buf):
        """ send a command as an indication: don't wait for procssing """
        cmd = self.Command(words=buf.split(b' '), event=None)
        self._cmdqueue.put(cmd)

    def mac_send_command_check_status(self, buf):
        (s, w) = self.macll_send_command(buf.split(b' '))
        if s != self.Result.STATUS_OK:
            raise self.RadioError(s.name)

    def mac_get_class(self):
        r = self.macll_send_command_get_response(b"mac get class")
        self.log.debug("current mac class is %s", r)
        return r

    def mac_get_channel_status(self, iChannel):
        r = self.macll_send_command_get_response(b"mac get ch status %d" % iChannel)
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

    def mac_get_status_uncorrected(self):
        r = self.macll_send_command_get_response(b"mac get status")
        v = int(r, base=16)
        self.log.debug("mac status: %x", v)
        return self.MacStatus(v)

    def mac_get_devaddr(self):
        r = self.macll_send_command_get_response(b"mac get devaddr")
        v = int(r, base=16)
        self.log.debug("mac devaddr: %x", v)
        return v

    def mac_get_status(self):
        """
        get status, toggling is_joined according to state of devaddr
        
        I observed once that the status was not correct; so this seems like a good
        idea.
        """ 
        status = self.mac_get_status_uncorrected()

        devaddr = self.mac_get_devaddr()
        if devaddr != 0:
            status.value |= 1 << 4
        else:
            status.value &= ~(1 << 4)
        return status

    def mac_force_enable(self):
        """ send `mac forceENABLE` """
        self.mac_send_command_check_status(b"mac forceENABLE")

    def mac_set_class(self, macclass):
        """ set the mac class to macclass; must be 'A', 'B', or 'C' """
        self.mac_send_command_check_status(b"mac set class " + macclass)

    def mac_set_channel_status(self, iChannel, state):
        """ set the channel enable status for iChannel: True -> enable """
        v = b"on" if state else b"off"
        self.mac_send_command_check_status(b"mac set ch status %d %s" % (iChannel, v))
    
    def mac_save(self):
        """ tell the mac to save current configuration """
        self.mac_send_command_check_status(b"mac save")

    def mac_join(self, mode):
        """ tell the mac to join in specified mode: 'otaa' or 'abp' """
        self.mac_send_command_check_status(b"mac join %s" % mode)

    def write(self, port, buf):
        """ send the characters in buf to port; unconfirmed """
        b = [ b'mac', b'tx', b'uncnf', b"%d" % port, binascii.hexlify(buf) ].join(b' ')
        self.log.debug("queue uplink command: %s", str(b), encoding='ascii')
        self.macll_send_command_indication(b)

