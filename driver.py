"""
Copyright (C) 2019-2025 Alexandr Vasilyev, f-caro, Fern Lane and other
10moons-driver and this fork contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import argparse
import logging
from array import array
from typing import Any

import usb
import yaml
from evdev import AbsInfo, UInput, ecodes

# NOTE: For evdev docs pls see <https://python-evdev.readthedocs.io/en/latest/>

__version__ = "2.0.dev0"

CONFIG_DEFAULT_PATH = "config-vin1060plus.yaml"
LOGGING_FMT = "[%(asctime)s] [%(levelname).1s] [%(lineno)3s] %(message)s"
LOGGING_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _parse_config(config_path: str) -> dict[str, Any]:
    """Parses config from YAML file

    Args:
        config_path (str): path to config file

    Returns:
        dict[str, Any]: parsed config as dictionary object
    """
    with open(config_path, "r", encoding="utf-8") as file_io:
        config = yaml.load(file_io, yaml.FullLoader)
    return config


def _parse_args() -> argparse.Namespace:
    """Parses cli arguments

    Returns:
        argparse.Namespace: parsed arguments
    """
    parser = argparse.ArgumentParser(description="10moons T501-T503 driver")

    parser.add_argument(
        "-c",
        "--config",
        type=str,
        required=False,
        default=CONFIG_DEFAULT_PATH,
        help=f"path to config file (Default: {CONFIG_DEFAULT_PATH})",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        default=False,
        help="enable debug logs (overrides setting from config file)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
        help="show program's version number and exit",
    )

    return parser.parse_args()


def _prepare_device(
    vendor_id: int, product_id: int, reports: list[dict[str | int, list[int]]]
) -> tuple[usb.core.Device, usb.core.Endpoint]:
    """Finds and resets USB device

    Args:
        vendor_id (int): tablet's usb vendor ID (from lsusb)
        product_id (int): tablet's usb product ID (from lsusb)
        reports (list[dict[str | int, list[int]]]): array of SET_REPORTs from config

    Raises:
        Exception: in case of error (eg. insufficient permissions)

    Returns:
        tuple[usb.core.Device, usb.core.Endpoint]: usb device, interface endpoint (interfaces()[1].endpoints()[0])
    """
    # Find the device
    dev = usb.core.find(idVendor=vendor_id, idProduct=product_id)
    logging.debug(str(dev))

    # Check instance type
    if not isinstance(dev, usb.core.Device):
        raise Exception("USB device instance is not usb.core.Device type")

    # Select end point for reading second interface [2] for actual data
    # Interface[0] associated Internal USB storage (labelled as CDROM drive)
    # Interface[1] useful to map 'Full Tablet Active Area' -- outputs 64 bytes of xinput events
    # Interface[2] maps to the 'AndroidActive Area' -- outputs 8 bytes of xinput events

    # Reset the device (don't know why, but till it works don't touch it)
    logging.info("Resetting USB device")
    dev.reset()

    # Drop default kernel driver from all devices
    logging.info("Detaching kernel driver from USB device")
    for iface_id in [0, 1, 2]:
        if dev.is_kernel_driver_active(iface_id):
            logging.debug(f"Detaching kernel driver from interface: {iface_id}")
            dev.detach_kernel_driver(iface_id)

    # Set new configuration
    logging.info("Setting new configuration to USB device")
    dev.set_configuration()

    # Claim interface
    # Like in 10moons-tools: <https://github.com/DIGImend/10moons-tools>
    interface = 2
    logging.info(f"Claiming USB interface {interface}")
    usb.util.claim_interface(dev, interface)

    def _set_report(w_value, report_data) -> None:
        # Host to device, Class, Interface; SET_REPORT
        logging.debug(f"Sending SET_REPORT: {w_value}, {report_data}")
        dev.ctrl_transfer(0x21, 9, w_value, interface, report_data, timeout=250)

    # Send specific reports
    # From 10moons-tools: <https://github.com/DIGImend/10moons-tools>
    logging.info("Sending reports")
    for report in reports:
        for w_value, report_data in report.items():
            if isinstance(w_value, str):
                w_value = int(w_value)
            _set_report(w_value, report_data)

    # Find endpoint
    endpoint = dev[0].interfaces()[1].endpoints()[0]
    logging.debug(str(endpoint))

    return dev, endpoint


def _parse_ecodes(actions: dict[str | int, str], ensure_int: bool = True) -> dict[Any, list[int]]:
    """Converts string actions into arrays of ecodes

    Args:
        actions (dict[str  |  int, str]): pen_buttons or tablet_buttons from config
        ensure_int (bool, optional): always convert keys to integers. Defaults to True

    Returns:
        dict[str | int, list[int]]: {key code (same from config): [ecode 1, ecode 2, ...]}
    """
    ecodes_ = {}
    for key_code, action in actions.items():
        if ensure_int and isinstance(key_code, str):
            key_code = int(key_code)
        ecodes_[key_code] = [ecodes.ecodes[code_part] for code_part in action.split("+")]
    return ecodes_


def _create_uinputs(
    xinput_name: str,
    pen_ecodes: dict[int, list[int]],
    pen_touch_ecodes: dict[str, list[int]],
    btn_ecodes: dict[int, list[int]],
    pen_config: dict[str, int | bool],
) -> tuple[UInput, UInput]:
    """Creates virtual input devices for pen and tablet

    Args:
        xinput_name (str): device name
        pen_ecodes (dict[int, list[int]]): output of _parse_ecodes()
        pen_touch_ecodes (dict[str, list[int]]): output of _parse_ecodes()
        btn_ecodes (dict[int, list[int]]): output of _parse_ecodes()

    Raises:
        Exception: in case of error (eg. insufficient permissions)

    Returns:
        tuple[UInput, UInput]: pen, btn
    """
    # Parse ecodes
    pen_codes = []
    for value in pen_ecodes.values():
        pen_codes.extend(value)
    for value in pen_touch_ecodes.values():
        pen_codes.extend(value)
    btn_codes = []
    for value in btn_ecodes.values():
        btn_codes.extend(value)

    # Build pen events
    abs_info_x = AbsInfo(
        0,
        pen_config.get("min_x", 0),
        pen_config.get("max_x", 4095),
        0,
        0,
        pen_config.get("resolution_x", 1),
    )
    abs_info_y = AbsInfo(
        0,
        pen_config.get("min_y", 0),
        pen_config.get("max_y", 4095),
        0,
        0,
        pen_config.get("resolution_y", 1),
    )
    pressure_info = AbsInfo(
        0,
        pen_config.get("pressure_out_min", 0),
        pen_config.get("pressure_out_max", 2047),
        0,
        0,
        pen_config.get("resolution_pressure", 1),
    )
    pen_events = {
        ecodes.EV_KEY: pen_codes,
        ecodes.EV_ABS: [(ecodes.ABS_X, abs_info_x), (ecodes.ABS_Y, abs_info_y), (ecodes.ABS_PRESSURE, pressure_info)],
    }
    logging.debug(f"PEN events: {pen_events}")

    # Build button events
    btn_events = {ecodes.EV_KEY: btn_codes}
    logging.debug(f"BTN events: {btn_events}")

    # Create virtual pen
    logging.info(f"Creating UInput {xinput_name}")
    virtual_pen = UInput(events=pen_events, name=xinput_name, version=0x3)
    logging.debug(str(virtual_pen))
    logging.debug(virtual_pen.capabilities(verbose=True).keys())
    logging.debug(virtual_pen.capabilities(verbose=True))

    # Create virtual buttons
    logging.info(f"Creating UInput {xinput_name}_buttons")
    virtual_btn = UInput(events=btn_events, name=xinput_name + "_buttons", version=0x3)
    logging.debug(str(virtual_btn))
    logging.debug(virtual_btn.capabilities(verbose=True).keys())
    logging.debug(virtual_btn.capabilities(verbose=True))

    return virtual_pen, virtual_btn


def _write_ecode(device: UInput, ecodes_: list[int], press: bool = True) -> None:
    """Writes ecodes to device

    Args:
        device (UInput): virtual input device
        ecodes_ (list[int]): list of ecodes
        press (bool, optional): True to write as 1, False to write as 0. Defaults to True
    """
    for ecode_ in ecodes_:
        logging.debug(f'{"Pressing" if press else "Releasing"} ecode: {ecode_}')
        device.write(ecodes.EV_KEY, ecode_, 1 if press else 0)
    device.syn()


def main() -> None:
    """Main entry and main loop"""
    # Parse CLI arguments and config file
    args = _parse_args()
    config = _parse_config(args.config)

    # Setup logging
    logging.basicConfig(
        format=LOGGING_FMT,
        datefmt=LOGGING_DATEFMT,
        level=logging.DEBUG if args.debug or config.get("debug") else logging.INFO,
    )
    logging.info(f"driver-vin1060plus version: {__version__}")

    logging.debug("DEBUG MODE ENABLED")
    logging.debug(f"Parsed config: {config}")

    # Parse actions
    actions_conf = config.get("actions", {})
    pen_ecodes: dict[int, list[int]] = _parse_ecodes(actions_conf.get("pen_buttons", {}))
    logging.debug(f"Pen ecodes: {pen_ecodes}")
    btn_ecodes: dict[int, list[int]] = _parse_ecodes(actions_conf.get("tablet_buttons", {}))
    logging.debug(f"Button ecodes: {btn_ecodes}")
    pen_touch_ecodes: dict[str, list[int]] = _parse_ecodes(actions_conf.get("pen_touch", {}), ensure_int=False)
    logging.debug(f"Pen touch ecodes: {pen_touch_ecodes}")

    # Prepare USB device
    try:
        dev, endpoint = _prepare_device(config["vendor_id"], config["product_id"], config["reports"])
    except Exception as e:
        logging.error("Error preparing tablet USB device", exc_info=e)
        logging.info("TIP: Make sure that tablet is connect and you running this script as root")
        return

    pen_config = config.get("pen", {})

    # Create virtual devices
    try:
        virtual_pen, virtual_btn = _create_uinputs(
            config.get("xinput_name", "10moons-pen"), pen_ecodes, pen_touch_ecodes, btn_ecodes, pen_config
        )
    except Exception as e:
        logging.error("Error creating virtual input devices", exc_info=e)
        logging.info("TIP: Make sure to run this script as root")
        return

    # Pre-parse from config
    swap_axes = pen_config.get("swap_axes")
    invert_x = pen_config.get("invert_x")
    invert_y = pen_config.get("invert_y")
    min_x = pen_config.get("min_x", 0)
    max_x = pen_config.get("max_x", 4095)
    min_y = pen_config.get("min_y", 0)
    max_y = pen_config.get("max_y", 4095)
    pressure_in_min = pen_config.get("pressure_in_min", 2047)
    pressure_in_max = pen_config.get("pressure_in_max", 0)
    pressure_out_min = pen_config.get("pressure_out_min", 0)
    pressure_out_max = pen_config.get("pressure_out_max", 2047)
    pressure_threshold_press = pen_config.get("pressure_threshold_press", 300)
    pressure_threshold_release = pen_config.get("pressure_threshold_release", 200)

    # Loop variables
    touch = False
    btn_pen_key_last = None
    btn_tablet_key_last = None

    # Main loop
    logging.info("Entering main loop. Press CTRL+C to stop driver and exit")
    while True:
        try:
            # Read data from tablet
            data = dev.read(endpoint.bEndpointAddress, endpoint.wMaxPacketSize)  # pyright: ignore
            logging.debug(f"[RAW] USB data: {data}")

            # Check type (just in case)
            if not isinstance(data, array):
                raise Exception("USB data type is not array.array")

            # Parse tablet key
            # TODO: Properly understand tablet key, because it's obviously not a big int type
            key_raw = int.from_bytes(data[11:13], byteorder="big")
            logging.debug(f"[RAW] Tablet key: {key_raw}")

            # Tablet key released
            if btn_tablet_key_last is not None and key_raw != btn_tablet_key_last:
                logging.debug(f"Tablet key {btn_tablet_key_last} released")
                _write_ecode(virtual_btn, btn_ecodes.get(btn_tablet_key_last, []), press=False)
                btn_tablet_key_last = None

            # Tablet key pressed
            if key_raw in btn_ecodes:
                logging.debug(f"Tablet key {key_raw} pressed")
                _write_ecode(virtual_btn, btn_ecodes.get(key_raw, []))
                btn_tablet_key_last = key_raw

            # Parse action
            pen_action = data[5]
            logging.debug(f"[RAW] pen action: {pen_action}")
            if pen_action not in [3, 4, 5, 6]:
                logging.debug("Ignoring this pen action")
                continue

            # Parse raw position and pressure
            x = int.from_bytes(data[3:5] if swap_axes else data[1:3], byteorder="big")
            y = int.from_bytes(data[1:3] if swap_axes else data[3:5], byteorder="big")
            pressure_raw = int.from_bytes(data[5:7], byteorder="big")
            logging.debug(f"[RAW] X: {x}, Y: {y}, swapped: {'true' if swap_axes else 'false'}")
            logging.debug(f"[RAW] Pressure: {pressure_raw}")

            # Parse pen button
            pen_btn_raw = data[9]
            logging.debug(f"[RAW] Pen button: {pen_btn_raw}")

            # Check position
            if x <= min_x or x >= max_x or y <= min_y or y >= max_y:
                logging.debug("Position is outside allowed range. Ignoring")
                continue

            # Invert axes
            if invert_x:
                x = max_x - x
            if invert_y:
                y = max_y - y

            # Calculate and clamp pressure
            pressure = pressure_raw - pressure_in_min
            pressure *= pressure_out_max - pressure_out_min
            pressure /= pressure_in_max - pressure_in_min
            pressure += pressure_out_min
            pressure = int(max(pressure_out_min, min(pressure, pressure_out_max)))

            # Check thresholds
            if not touch and pressure > pressure_threshold_press:
                touch = True
            elif touch and pressure < pressure_threshold_release:
                touch = False

            logging.debug(f"[OUT] X: {x}, Y: {y}, pressure: {pressure}, touch: {touch}")

            # Write touch
            for ecode_ in pen_touch_ecodes.get("down" if touch else "up", []):
                virtual_pen.write(ecodes.EV_KEY, ecode_, 1 if touch else 0)
            virtual_pen.syn()

            # Write position and pressure
            virtual_pen.write(ecodes.EV_ABS, ecodes.ABS_X, x)
            virtual_pen.write(ecodes.EV_ABS, ecodes.ABS_Y, y)
            virtual_pen.write(ecodes.EV_ABS, ecodes.ABS_PRESSURE, pressure)
            virtual_pen.syn()

            # Pen button released
            if btn_pen_key_last is not None and pen_btn_raw != btn_pen_key_last:
                logging.debug("Pen button released")
                _write_ecode(virtual_pen, pen_ecodes.get(btn_pen_key_last, []), press=False)
                btn_pen_key_last = None

            # Pen button pressed
            if btn_pen_key_last is None and pen_btn_raw in pen_ecodes:
                logging.debug("Pen button pressed")
                _write_ecode(virtual_pen, pen_ecodes.get(pen_btn_raw, []))
                btn_pen_key_last = pen_btn_raw

        # USB error
        except usb.core.USBError as e:
            # Just timed out. Ignoring it
            if e.args[0] == 110:
                continue

            # Disconnected or other error
            if e.args[0] == 19:
                logging.warning("Device disconnected")
            else:
                logging.warning(f"USB error: {e}")
            break

        # CTRL+C
        except (SystemExit, KeyboardInterrupt):
            logging.warning("Exiting ...")
            break

        # Some other error
        except Exception as e:
            logging.error(f"Unknown error: {e}", exc_info=e)
            break

    # Release pen and tablet keys
    try:
        if touch:
            for ecode_ in pen_touch_ecodes.get("up", []):
                virtual_pen.write(ecodes.EV_KEY, ecode_, 0)
            virtual_pen.syn()
        if btn_pen_key_last is not None:
            _write_ecode(virtual_pen, btn_ecodes.get(btn_pen_key_last, []), press=False)
        if btn_tablet_key_last is not None:
            _write_ecode(virtual_btn, btn_ecodes.get(btn_tablet_key_last, []), press=False)
    except Exception as e:
        logging.warning(f"Unable to release pen and tablet keys: {e}")

    # Close devices on exit
    logging.info("Closing virtual input devices")
    try:
        virtual_pen.close()
        virtual_btn.close()
    except Exception as e:
        logging.warning(f"Unable to close virtual input devices: {e}")

    logging.info("Exited!")


if __name__ == "__main__":
    main()
