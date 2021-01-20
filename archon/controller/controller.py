#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2021-01-19
# @Filename: archon.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import re
import warnings

from clu.device import Device

from archon.controller.command import ArchonCommand
from archon.exceptions import ArchonUserWarning

__all__ = ["ArchonController"]


class ArchonController(Device):
    """Talks to an Archon controller over TCP/IP.

    Parameters
    ----------
    host
        The hostname of the Archon.
    port
        The port on which the Archon listens to incoming connections.
        Defaults to 4242.
    """

    __running_commands: dict[int, ArchonCommand] = {}
    __next_id = 0

    def __init__(self, host: str, port: int = 4242):
        super().__init__(host, port)

    def send_command(
        self,
        command_string: str,
        command_id: int | None = None,
        expected_replies: int = 1,
    ) -> ArchonCommand:
        """Sends a command to the Archon.

        Parameters
        ----------
        command_string
            The command to send to the Archon. Will be converted to uppercase.
        command_id
            The command id to associate with this message. If not provided, a
            sequential, autogenerated one will be used.
        expected_replies
            How many replies to expect from the controller before the command is done.
        """
        command_id = command_id or self.__get_id()

        command = ArchonCommand(
            command_string, command_id, expected_replies=expected_replies
        )
        self.__running_commands[command_id] = command

        self.write(command.raw)

        return command

    async def process_message(self, line: bytes) -> None:
        """Processes a message from the Archon and associates it with its command."""
        match = re.match(b"^[<|?]([0-9A-F]{2})", line)
        if match is None:
            warnings.warn(
                f"Received invalid command {line.decode()}", ArchonUserWarning
            )

        command_id = int(match[0], 16)
        if command_id not in self.__running_commands:
            warnings.warn(f"Cannot find running command for {line}")
            return

        self.__running_commands[command_id].process_reply(line)

    def __get_id(self) -> int:
        """Returns an identifier and increases the counter."""
        id = self.__next_id
        self.__next_id += 1
        return id
