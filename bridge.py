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
import pty
import os
import queue
import serial
import re
import subprocess
import sys
import threading

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
    return parser.parse_args()

##############################################################################
#
# Spawn a captive shell and return a file handle.
#
##############################################################################

def spawn_captive_shell():
    """spawn captive shell and return file handle"""
    argv = [ "/bin/bash", "--login" ]
    (childpid, parentfd) = pty.fork()
    if childpid == 0:
        # we're the child
        try:
            print(f"Spawning {argv[0]}...")
            os.execvp(argv[0], argv)
        except OSError as err:
            print(f"Cannot execute {argv[0]}: {str(err)}")
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
# A simple class that implements a non-blocking API for pty subprocess I/O
#
##############################################################################

class App():
    def __init__(self, args):
        self.args = args

        # init a connection to the local port.
        try:
            if self.args.local == "PTY":
                # create a process and get handles
                (_, fdForParentUse) = spawn_captive_shell()
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
            print(f"Failed to open local port: {err}")
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
                print("downlink:", thisMessage)
                self.local.write(binascii.unhexlify(thisMessage))
            else:
                print("ignored:", thisLine.decode('utf-8', 'backslashreplace'))

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
                print("uplink:", binascii.hexlify(buf))
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
