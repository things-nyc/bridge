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

from rn2903 import Rn2903

### system libraries ###
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
    return parser.parse_args()

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

        logging.basicConfig(level=loglevel, format='%(relativeCreated)6d %(threadName)s %(levelname)-6s %(message)s')
        self.log = logging
        self.log.debug("Initializing App")

        self.radio = Rn2903(self.args.radio, baudrate=self.args.rbaud, loglevel=logging.INFO)

        self.initialize_radio()
        self.log.info("Radio is initialized")

        return

    def initialize_radio(self):
        need_set = False

        macclass = self.radio.mac_get_class()
        if macclass != "C":
            self.log.info("mac class is not C: %s", macclass)
            need_set = True

        macstatus = self.radio.mac_get_status()
        if macstatus.is_silent():
            self.log.info("mac was silenced, turn it on")
            self.radio.mac_force_enable()

        if (not need_set) and not macstatus.need_join():
            self.log.info("Already joined as class C, assume radio state is corect")
            return

        desired_mask = (int(1) << 65) | (int(0xFF) << 8)
        mask = self.radio.mac_get_channel_mask()
        if mask != desired_mask:
            self.log.info("mac class mask %x is not %x", mask, desired_mask)
            need_set = True

        # do required init
        if need_set:
            if macclass != "C":
                self.radio.mac_set_class("C")
            diff_mask = desired_mask ^ mask
            for iChannel in range(72):
                iChannel_bit = int(1) << iChannel
                if diff_mask & iChannel_bit != 0:
                    self.radio.mac_set_channel_status(iChannel, (desired_mask & iChannel_bit) != 0)
            self.radio.mac_save()
            self.radio.mac_join(b"otaa")
        elif macstatus.need_join():
            self.radio.mac_join(b"otaa")

def main():
    global gApp
    args = ParseArguments()
    gApp = App(args)

    try:
        gApp.radio.wait_for_exit()
    except:
        if gApp.radio.is_running():
            gApp.radio.request_exit()

        subprocess.run(["stty", "sane"])
        raise
    return 0

if __name__ == '__main__':
    sys.exit(main())
