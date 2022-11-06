#!/usr/bin/env python3

import sys
import time

import usb.core
import usb.util

crc = None
try:
    import crc
except ModuleNotFoundError:
    print("Warning: crc package not found. crc checks will not be performed", file=sys.stderr)

# Bus 001 Device 020: ID 0483:003a STMicroelectronics FNB-48
VID = 0x0483
PID_FNB48 = 0x003A

# C1
# Bus 001 Device 029: ID 0483:003b STMicroelectronics USB Tester
PID_C1 = 0x003B

# FNB58
VID_FNB58 = 0x2E3C
PID_FNB58 = 0x5558

#  iManufacturer           1 FNIRSI
#  iProduct                2 FNB-48
#  iSerial                 3 0001A0000000


def setup_crc():
    if crc is None:
        return None
    # $ ./reveng -w 8 -s $(shuf -n 100 /tmp/dump2.txt)
    # width=8  poly=0x39  init=0x42  refin=false  refout=false  xorout=0x00  check=0x4b  residue=0x00  name=(none)
    width = 8
    poly = 0x39
    init_value = 0x42
    final_xor_value = 0x00
    reverse_input = False
    reverse_output = False
    configuration = crc.Configuration(width, poly, init_value, final_xor_value, reverse_input, reverse_output)
    use_table = True
    crc_calculator = crc.CrcCalculator(configuration, use_table)
    return crc_calculator


def main():
    # find our device
    is_fnb58 = False
    dev = usb.core.find(idVendor=VID, idProduct=PID_FNB48)
    if dev is None:
        dev = usb.core.find(idVendor=VID, idProduct=PID_C1)
    if dev is None:
        dev = usb.core.find(idVendor=VID_FNB58, idProduct=PID_FNB58)
        is_fnb58 = True
    assert dev, "Device not found"

    if False:
        print(dev, file=sys.stderr)

    dev.reset()

    # if dev.is_kernel_driver_active(0):
    #        try:
    #                dev.detach_kernel_driver(0)
    #        except usb.core.USBError as e:
    #                sys.exit("Could not detach kernel driver: ")

    # https://github.com/pyusb/pyusb/issues/76#issuecomment-118460796
    intf_hid = 0
    for cfg in dev:
        for intf in cfg:
            if intf.bInterfaceClass == 0x03:  # HID class
                intf_hid = intf.bInterfaceNumber

            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                try:
                    dev.detach_kernel_driver(intf.bInterfaceNumber)
                except usb.core.USBError as e:
                    sys.exit(f"Could not detatch kernel driver from interface({intf.bInterfaceNumber}): {e}")

    usb.util.claim_interface(dev, 0)

    # set the active configuration. With no arguments, the first
    # configuration will be the active one

    if False:
        for cfg in dev:
            print("cfg", cfg.bConfigurationValue, file=sys.stderr)
            for intf in cfg:
                print(
                    "   ",
                    "intf",
                    intf.bInterfaceNumber,
                    intf.bAlternateSetting,
                    file=sys.stderr,
                )
                for ep in intf:
                    print("         ", "ep", ep.bEndpointAddress, file=sys.stderr)

    # dev.set_configuration(1)

    # get an endpoint instance
    cfg = dev.get_active_configuration()
    intf = cfg[(intf_hid, 0)]

    ep_out = usb.util.find_descriptor(
        intf,
        # match the first OUT endpoint
        custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT,
    )

    ep_in = usb.util.find_descriptor(
        intf,
        # match the first IN endpoint
        custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN,
    )

    assert ep_in
    assert ep_out

    ep_out.write(b"\xaa\x81" + b"\x00" * 61 + b"\x8e")
    ep_out.write(b"\xaa\x82" + b"\x00" * 61 + b"\x96")

    if is_fnb58:
        ep_out.write(b"\xaa\x82" + b"\x00" * 61 + b"\x96")
    else:
        ep_out.write(b"\xaa\x83" + b"\x00" * 61 + b"\x9e")

    alpha = 0.9  # smoothing factor for temperature
    temp_ema = None

    # At the moment only 100 sps is supported
    sps = 100
    time_interval = 1.0 / sps

    energy = 0.0
    capacity = 0.0

    crc_calculator = setup_crc()  # can be None

    print()  # Extra line to concatenation work better in gnuplot.
    print("timestamp sample_in_packet voltage_V current_A dp_V dn_V temp_C_ema energy_Ws capacity_As")

    def decode(data):
        nonlocal temp_ema, energy, capacity

        # Data is 64 bytes (64 bytes of HID data minus vendor constant 0xaa)
        # First byte is HID vendor constant 0xaa
        # Second byte is payload type:
        #    0x04 is data packet
        #    Other types (0x03 and maybe other ones) is unknown
        # Next 4 samples each 15 bytes. 60 bytes total.
        # At the end 2 bytes:
        #   1 byte is semi constant with unknown purpose.
        #   1 byte (last) is a 8-bit CRC checksum

        packet_type = data[1]
        if packet_type != 0x04:
            # ignore all non-data packets
            # print("Ignoring")
            return

        if crc_calculator:
            actual_checksum = data[-1]
            expected_checksum = crc_calculator.calculate_checksum(data[1:-1])
            if actual_checksum != expected_checksum:
                print(
                    f"Ignoring packet of length {len(data)} with unexpected checksum. Expected: {expected_checksum:02x} Actual: {actual_checksum:02x}",
                    file=sys.stderr,
                )
                return

        t0 = time.time() - 4 * time_interval
        for i in range(4):
            offset = 2 + 15 * i
            # 4 + 4 + 2 + 2 + 2 + 1 (unknown constant 1) = 15 bytes
            voltage = (
                data[offset + 3] * 256 * 256 * 256
                + data[offset + 2] * 256 * 256
                + data[offset + 1] * 256
                + data[offset + 0]
            ) / 100000
            current = (
                data[offset + 7] * 256 * 256 * 256
                + data[offset + 6] * 256 * 256
                + data[offset + 5] * 256
                + data[offset + 4]
            ) / 100000
            dp = (data[offset + 8] + data[offset + 9] * 256) / 1000
            dn = (data[offset + 10] + data[offset + 11] * 256) / 1000
            # unknown12 = data[offset + 12]  # ? constant 1  # some PD info?
            # It does not look to be a sign of current. I tried reversing
            # USB-C-In and USB-C-Out, which does reversed orientation of the
            # blue arrow for current on device screen, but unknown12 remains 1.
            # print(f"unknown{offset+12} {unknown12:02x}")
            temp_C = (data[offset + 13] + data[offset + 14] * 256) / 10.0
            if temp_ema is not None:
                temp_ema = temp_C * (1.0 - alpha) + temp_ema * alpha
            else:
                temp_ema = temp_C
            power = voltage * current
            # TODO(baryluk): This might be slightly inaccurate, if there is sampling jitter.
            energy += power * time_interval
            capacity += current * time_interval
            t = t0 + i * time_interval
            print(
                f"{t:.3f} {i} {voltage:7.5f} {current:7.5f} {dp:5.3f} {dn:5.3f} {temp_ema:6.3f} {energy:.6f} {capacity:.6f}"
            )
        # unknown62 = data[62]  # data[-2]
        # print(f"unknown62 {unknown:02x}")
        # print()

    time.sleep(0.1)
    refresh = 1.0 if is_fnb58 else 0.003  # 1 s for FNB58, 3 ms for others
    continue_time = time.time() + refresh
    while True:
        data = ep_in.read(size_or_buffer=64, timeout=1000)
        # print("".join([f"{x:02x}" for x in data]))
        decode(data)

        if time.time() >= continue_time:
            continue_time = time.time() + refresh
            ep_out.write(b"\xaa\x83" + b"\x00" * 61 + b"\x9e")


if __name__ == "__main__":
    main()
