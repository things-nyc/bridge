# LoRaWAN to RS232 Bridge

The `bridge` program connects a [Microchip RN2903](https://www.microchip.com/en-us/product/RN2903)-based LoRaWAN device to one or more local serial ports, allowing low bandwidth remote monitoring and control of things like network routers (our initial use case with [NYC Mesh](https://www.nycmesh.net)).

`bridge` uses the command set used by the RN2903, and so it's not directly useable with other adapters. However, all the specifics are isolated in a radio driver with an abstract interface, so it should not be difficult to adapt this code to work with other radios.

`bridge` been tested with the [NYC-duino](https://github.com/things-nyc/nyc-duino) and the [Microchip RN2903 Mote](https://www.microchip.com/en-us/development-tool/DM164139).

Here's a diagram of the intended use case:

<!-- see deployment.puml for the source code, or decode the URL -->
![UML Deployment Diagram](https://www.plantuml.com/plantuml/svg/VPDHRzCm4CVV_IbEtSkGQ5W3Um0Xj55UG115eHCFOmzkV6sifRPrTbhrs-EBPzR0LAbgOz_lV__BtVN61qbXsFL2ji4It7aaqTgTimPDW5czd87qK2zFBx_RHlwwhQ2HIjin_iC6F2KQwTqQYOvGwz_cykxdP-Yi3wzIClsCHiFr0ku5G6JcmSwRR--kusbfpHuf8C75GZnC-V8yN_xBL-VvQiBF6Ziasx7MT5gy19GdGFaIK9q0bJ1M8SnM7SAgqsRheS9miFGuGgjL9ThU3YetDsbpeo_yut7T3vYPDMcrnS8TuJ9qseCZkoMvI-rDGRZOsbvbmU2Hvj8v1iOPtphtH0W-mlmJpxXUGa6wA3B25tDbOp0M21_Wgmb81eFWuySnaciKwJTVfuqG_9rkeDOnGHo2C7pNuoQ0tIGCk2KUuaSQQAho_TLREEZGmNvHN5t3HjFk80bZL4LMvb5w92rxa4gwW5G8D0hCQ1kzqdkaNeYfPwr7HumFx1dYKrxGr-p1xTnhXHwSFI15EDYHuc8PoEUJbNfoyiLJQaba3nwqKRgLKQKqOHkqDLn0JikfsFMDmhrk4GXxoOplj2lWYmmIDdrC4z6r_fj1zlrn-h9fHkclb7hCZtaIQG4vmeNovfkOoD94M3uBowcmF2-ideNLL5Zz9qnb5VUTLWhDGzgqQ9XSYtnceVHW41KgSOD63Rl-3m00)

## Modem and Modem Setup

LoRaWAN modems generally have significant stored state. During startup, `bridge` queries the state of the modem and tries to ensure that it's in the proper state:

| Desired state | Action taken to enforce |
|---------------|-------------------------|
| Not administratively silenced | Turn off the administrative-silence state.
| Class C operations enabled | Set MAC to class C. |
| Already joined to the network. | Set up the network channel mask, and then join.
| A class A uplink has been sent (to cause The Things Stack to enable Class C) | Send a dummy message to port 42.

During initialization, The RN2903 is configured in Class C mode using `mac set class c`. If the RN2903 is not yet joined (based on `devaddr` being `0`), the channel mask is set to 8~15/65, and then `mac join` is issued.

## Program flow

Throughout the description that follows, we assume that the program is properly executing the protocol for communicating with the LoRaWAN radio. It's not enough to send a line of text; you have to get the response from the radio and know whether the radio is ready to accept another command. For example, a `mac join otaa` request gets an immediate `ok` response, but you actually need to wait for an additional `accepted` or `denied` response before the radio is ready for the next command. Similarly, an uplink using `mac tx uncnf {port} ...` gets at least two responses; first an `ok` to indicate that the transmit has been accepted by the radio, and later a `mac_tx_ok` when the full uplink completes.

The program has two distinct phases of operation.

1. **Initialization:** the LoRaWAN radio is located and probed. Any necessary initialization that we can perform is performed -- we make sure the radio is in Class C mode, we launch a join to the network, and so forth. Critically, we don't leave this phase until the join is accepted. Because of the peculiarities of class C operation, we send a dummy uplink to the network.

2. **Operation:** the program monitors all the serial ports concurrently.

   * Unsolicited messages from the radio are checked to see if they're downlinks. If so, the port number is used to select a given serial port/router, and the data is forwarded.

   * Characters from the router serial ports are collected into uplinks that are sent on port `n`, where `n` is the router number. Because of the long delays involved in LoRaWAN uplinks, we have to maintain a queue of commands to the radio; this handles both long messages from an individual router, and situations where multiple routers talk at the same time (for example, after a power failure).

   We guess that the uplink direction may require a limited queue depth to avoid denial of service if multiple routers are jabbering concurrently.

   We may need in the future to allow the LoRaWAN network to quench all routers except one of interest. This is not implemented yet.

## Radio driver

The application spawns a thread as a simple driver for the RN2903. The thread manages all communication with the 2903. It receives serializes commands via a queue, and dispatches downlinks via a separate queue.

Here's a simple FSM:

![Driver FSM](https://www.plantuml.com/plantuml/svg/RP3DIaGn38NtVOginSliNSY88FWCugAs1mdsvzgaWoA-kvaxjZEBMJK_tqcQinVrJNjEpW85YJuNLlOZVmZA1z2F8vf0JBX87tDqlywztBpI4fVxYmapcesZqfrUkgErrG0HwaLgui2AI0sorYAGoItDjDiQamX08SUtz45SwBEZmxdHHD7slJEcH0erPs-lLier8azezKrmNaC-XDeYbtT3Xsh2cpcadD7-QqkQbvo-CqVHxYXV4nItLWprS5sqZQjTp_pqPFZqnCKd8_75IWrsfgWRvOqnjzqU9VQS_W00)

## To do

As of this writing, the code is still a very raw prototype. See the list of issues in the repository.
