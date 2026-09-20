#!/usr/bin/env python3
# Copyright (c) 2026 Sascha Ludwig, astrastudio broadcast solutions
# SPDX-License-Identifier: MIT

"""Unit tests for Sony RM-IP setup packet encoding and parsing.

These tests stay offline: they only check frame layout, field parsing
and input validation. Live UDP discovery needs a camera on the LAN.
"""

import unittest

from sony_camera_ip_setup import (
    CameraInfo,
    RmIpError,
    apply_network_setting,
    build_inquiry,
    build_network_setting,
    decode_fields,
    encode_fields,
    normalize_mac,
    parse_camera_reply,
    parse_setting_reply,
    select_camera,
    validate_name,
)


class PacketTests(unittest.TestCase):
    """Sony setup frames use STX, 0xFF separators and ETX."""

    def test_inquiry_frame(self):
        """Inquiry must be ENQ:network inside a standard setup frame."""
        packet = build_inquiry()
        self.assertEqual(packet[0], 0x02)
        self.assertEqual(packet[-1], 0x03)
        self.assertEqual(packet[-2], 0xFF)
        self.assertEqual(decode_fields(packet), ["ENQ:network"])

    def test_network_setting_frame(self):
        """Setting packets use IPADR and a hyphenated uppercase MAC."""
        packet = build_network_setting(
            "aa:bb:cc:dd:ee:ff",
            "192.168.42.52",
            "255.255.255.0",
            "192.168.42.1",
            "CAM1",
        )
        self.assertEqual(
            decode_fields(packet),
            [
                "MAC:AA-BB-CC-DD-EE-FF",
                "IPADR:192.168.42.52",
                "MASK:255.255.255.0",
                "GATEWAY:192.168.42.1",
                "NAME:CAM1",
            ],
        )

    def test_parse_inquiry_reply(self):
        """Inquiry replies map official field names onto CameraInfo."""
        packet = encode_fields(
            [
                "MAC:00-11-22-33-44-55",
                "MODEL:IPCARD",
                "SOFTVERSION:1.00.00",
                "IPADR:192.168.0.100",
                "MASK:255.255.255.0",
                "GATEWAY:192.168.0.1",
                "NAME:CAM1",
                "WRITE:on",
            ]
        )
        camera = parse_camera_reply(packet)
        self.assertIsNotNone(camera)
        self.assertEqual(camera.mac, "00-11-22-33-44-55")
        self.assertEqual(camera.ip, "192.168.0.100")
        self.assertEqual(camera.mask, "255.255.255.0")
        self.assertEqual(camera.gateway, "192.168.0.1")
        self.assertEqual(camera.name, "CAM1")
        self.assertEqual(camera.model, "IPCARD")
        self.assertTrue(camera.writable)

    def test_parse_ack_and_nak(self):
        """ACK is success; NAK may carry an extra detail field."""
        ack = encode_fields(["ACK:AA-BB-CC-DD-EE-FF"])
        status, mac, detail = parse_setting_reply(ack)
        self.assertEqual(status, "ACK")
        self.assertEqual(mac, "AA-BB-CC-DD-EE-FF")
        self.assertEqual(detail, "")

        nak = encode_fields(["NAK:AA-BB-CC-DD-EE-FF", "error"])
        status, mac, detail = parse_setting_reply(nak)
        self.assertEqual(status, "NAK")
        self.assertEqual(mac, "AA-BB-CC-DD-EE-FF")
        self.assertEqual(detail, "error")


class ValidationTests(unittest.TestCase):
    """Sony rejects invalid MACs and names before a packet is sent."""

    def test_normalize_mac(self):
        """Colon and hyphen input both become AA-BB-CC-DD-EE-FF."""
        self.assertEqual(normalize_mac("aa:bb:cc:dd:ee:ff"), "AA-BB-CC-DD-EE-FF")
        with self.assertRaises(RmIpError):
            normalize_mac("not-a-mac")

    def test_name_limits(self):
        """NAME is limited to 8 letters, digits or spaces."""
        self.assertEqual(validate_name("CAM 1"), "CAM 1")
        with self.assertRaises(RmIpError):
            validate_name("TOOLONGNAME")
        with self.assertRaises(RmIpError):
            validate_name("CAM#1")

    def test_select_camera_requires_unique_match(self):
        """Ambiguous matches must fail instead of changing a random camera."""
        cameras = [
            CameraInfo(
                "00-11-22-33-44-55",
                "192.168.0.100",
                "255.255.255.0",
                "0.0.0.0",
                "CAM1",
                writable=True,
            ),
            CameraInfo(
                "00-11-22-33-44-66",
                "192.168.0.101",
                "255.255.255.0",
                "0.0.0.0",
                "CAM2",
                writable=True,
            ),
        ]
        chosen = select_camera(cameras, None, "192.168.0.101", None)
        self.assertEqual(chosen.mac, "00-11-22-33-44-66")
        with self.assertRaises(RmIpError):
            select_camera(cameras, None, None, None)

    def test_apply_rejects_write_off(self):
        """Do not send a setting packet when Sony reports WRITE:off."""
        camera = CameraInfo(
            "00-11-22-33-44-55",
            "192.168.0.100",
            "255.255.255.0",
            "0.0.0.0",
            "CAM1",
            writable=False,
        )
        with self.assertRaises(RmIpError) as ctx:
            apply_network_setting(camera, "192.168.42.52")
        self.assertIn("WRITE", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
