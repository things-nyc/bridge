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
from xml.etree.ElementPath import get_parent_map
import re
import serial
import sys

def ParseArguments():
    parser = argparse.ArgumentParser(description="Bridge LoRaWAN class C modem to local comm port")
    parser.add_argument("--local", "-l", required=True, help="Local comm port", metavar="{comm}")
    parser.add_argument("--lbaud", default=115200, type=int, help="Baud rate for local port", metavar="{baudrate}")
    parser.add_argument("--remote", "-r", required=True, help="Local comm port", metavar="{comm}")
    parser.add_argument("--rbaud", default=57600, type=int, help="Baud rate for LoRaWAN modem port", metavar="{baudrate}")
    return parser.parse_args()

def Initialize(gApp):
    gApp['local'] = serial.Serial(
        port=gApp['args'].local,
        baudrate=gApp['args'].lbaud,
        timeout=0
        )

    # get rid of anything that might be waiting in the buffer
    gApp['local'].reset_input_buffer()

    # open the remote port
    gApp['remote'] = serial.Serial(
        port=gApp['args'].remote,
        baudrate=gApp['args'].rbaud,
        timeout=0
        )

    # get rid of anything that might be waiting in the buffer
    gApp['remote'].reset_input_buffer()

    gApp['data'] = b''
    gApp['downlinkPattern'] = re.compile(b'^mac_rx ([0-9]+) ([0-9A-F]*)$')
    return

def SaveData(gApp, buf):
    gApp['data'] += buf
    None

def LookForDownlink(gApp):
    lines = gApp['data'].split(b'\r\n')
    if lines[len(lines) - 1] != b'':
        gApp['data'] = lines.pop()
    else:
        gApp['data'] = b''
    for thisLine in lines:
        result = gApp['downlinkPattern'].match(thisLine)
        if result != None:
            # matched!
            thisMessage = result.group(2)
            print("downlink:", thisMessage)
            gApp['local'].write(binascii.unhexlify(thisMessage))
        else:
            print("ignored:", thisLine.decode('utf-8', 'backslashreplace'))

def PushRemoteToLocal(gApp):
    buf = gApp['remote'].read(256)
    if len(buf) > 0:
        SaveData(gApp, buf)
        LookForDownlink(gApp)
        #gApp['local'].write(buf)
    None

def PushLocalToRemote(gApp):
    buf = gApp['local'].read(128)
    if len(buf) > 0:
        print("uplink:", binascii.hexlify(buf))
        gApp['remote'].write(BuildTxMessage(1, buf))
    None

def BuildTxMessage(port, buf):
    result = b"mac tx uncnf 1 " + binascii.hexlify(buf) + b"\r\n"
    return result

def main():
    global gApp
    gApp = { }
    args = ParseArguments()
    gApp['args'] = args
    
    Initialize(gApp)
    while True:
        PushRemoteToLocal(gApp)
        PushLocalToRemote(gApp)
    return 0

if __name__ == '__main__':
    sys.exit(main())
