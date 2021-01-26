#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2021-01-19
# @Filename: archon.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import configparser
import os
import re
import warnings

from typing import Any, Callable, Optional

from clu.device import Device

from archon.controller.command import ArchonCommand
from archon.controller.maskbits import ModType
from archon.exceptions import ArchonError, ArchonUserWarning

from . import MAX_COMMAND_ID

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
    name
        A name identifying this controller.
    """

    __running_commands: dict[int, ArchonCommand] = {}
    __id_pool = set(range(MAX_COMMAND_ID))

    def __init__(self, host: str, port: int = 4242, name: str = ""):
        self.name = name
        super().__init__(host, port)

        # TODO: asyncio recommends using asyncio.create_task directly, but that
        # call get_running_loop() which fails in iPython.
        self._job = asyncio.get_event_loop().create_task(self.__track_commands())

    def send_command(
        self,
        command_string: str,
        command_id: Optional[int] = None,
        **kwargs,
    ) -> ArchonCommand:
        """Sends a command to the Archon.

        Parameters
        ----------
        command_string
            The command to send to the Archon. Will be converted to uppercase.
        command_id
            The command id to associate with this message. If not provided, a
            sequential, autogenerated one will be used.
        kwargs
            Other keyword arguments to pass to `.ArchonCommand`.
        """
        command_id = command_id or self.__get_id()
        if command_id > MAX_COMMAND_ID or command_id < 0:
            raise ArchonError(
                f"Command ID must be in the range [0, {MAX_COMMAND_ID:d}]."
            )

        command = ArchonCommand(
            command_string,
            command_id,
            **kwargs,
        )
        self.__running_commands[command_id] = command

        self.write(command.raw)

        return command

    async def send_many(
        self, cmd_strs: Iterable[str], max_chunk=100, timeout: Optional[float] = None
    ) -> tuple[list[ArchonCommand], list[ArchonCommand]]:
        """Sends many commands and waits until they are all done.

        If any command fails or times out, cancels any future command. Returns a list
        of done commands and a list failed commands (empty if all the commands
        have succeeded). Note that ``done+pending`` can be fewer than the length
        of ``cmd_strs``.

        Parameters
        ----------
        cmd_strs
            List of command strings to send. The command ids are assigned automatically
            from available IDs in the pool.
        max_chunk
            Maximum number of commands to send at once. After sending, waits until all
            the commands in the chunk are done. This does not guarantee that
            ``max_chunk`` of commands will be running at once, that depends on the
            available command ids in the pool.
        timeout
            Timeout for each single command.
        """
        cmd_strs = list(cmd_strs)  # Copy the strings so that we can pop them.
        done: list[ArchonCommand] = []

        while len(cmd_strs) > 0:
            pending: list[ArchonCommand] = []
            if len(cmd_strs) < max_chunk:
                max_chunk = len(cmd_strs)
            if len(self.__id_pool) >= max_chunk:
                cmd_ids = (self.__get_id() for __ in range(max_chunk))
            else:
                cmd_ids = (self.__get_id() for __ in range(len(self.__id_pool)))
            for cmd_id in cmd_ids:
                cmd_str = cmd_strs.pop()
                cmd = self.send_command(cmd_str, command_id=cmd_id, timeout=timeout)
                pending.append(cmd)
            done_cmds = await asyncio.gather(
                *pending,
                return_exceptions=True,
            )
            if all([cmd.succeeded() for cmd in done_cmds]):
                done += done_cmds
                for cmd in done_cmds:
                    self.__id_pool.add(cmd.command_id)
            else:
                failed: list[ArchonCommand] = []
                for cmd in done_cmds:
                    if cmd.succeeded():
                        done.append(cmd)
                    else:
                        failed.append(cmd)
                return done, failed

        return (done, [])

    async def process_message(self, line: bytes) -> None:
        """Processes a message from the Archon and associates it with its command."""
        match = re.match(b"^[<|?]([0-9A-F]{2})", line)
        if match is None:
            warnings.warn(f"Received invalid reply {line.decode()}", ArchonUserWarning)

        command_id = int(match[1], 16)
        if command_id not in self.__running_commands:
            warnings.warn(f"Cannot find running command for {line}", ArchonUserWarning)
            return

        self.__running_commands[command_id].process_reply(line)

    async def stop(self):
        """Stops the client and cancels the command tracker."""
        self._job.cancel()
        await super().stop()

    async def get_system(self) -> dict[str, Any]:
        """Returns a dictionary with the output of the ``SYSTEM`` command."""
        cmd = await self.send_command("SYSTEM", timeout=1)
        if not cmd.succeeded():
            raise ArchonError(f"Command finished with status {cmd.status.name!r}")

        keywords = str(cmd.replies[0].reply).split()
        system = {}
        for (key, value) in map(lambda k: k.split("="), keywords):
            system[key.lower()] = value
            if match := re.match(r"^MOD([0-9]{1,2})_TYPE", key, re.IGNORECASE):
                name_key = f"mod{match.groups()[0]}_name"
                system[name_key] = ModType(int(value)).name

        return system

    async def get_status(self) -> dict[str, Any]:
        """Returns a dictionary with the output of the ``STATUS`` command."""

        def check_int(s):
            if s[0] in ("-", "+"):
                return s[1:].isdigit()
            return s.isdigit()

        cmd = await self.send_command("STATUS", timeout=1)
        if not cmd.succeeded():
            raise ArchonError(f"Command finished with status {cmd.status.name!r}")

        keywords = str(cmd.replies[0].reply).split()
        status = {
            key.lower(): int(value) if check_int(value) else float(value)
            for (key, value) in map(lambda k: k.split("="), keywords)
        }

        return status

    async def get_frame(self) -> dict[str, int]:
        """Returns the frame information.

        All the returned values in the dictionary are integers in decimal
        representation.
        """
        cmd = await self.send_command("FRAME", timeout=1)
        if not cmd.succeeded():
            raise ArchonError(f"Command FRAME failed with status {cmd.status.name!r}")

        keywords = str(cmd.replies[0].reply).split()
        frame = {
            key.lower(): int(value) if "TIME" not in key else int(value, 16)
            for (key, value) in map(lambda k: k.split("="), keywords)
        }

        return frame

    async def read_config(
        self, save: str | bool = False, full: bool = False
    ) -> list[str]:
        """Reads the configuration from the controller.

        Parameters
        ----------
        save
            Save the configuration to a file. If ``save=True``, the configuration will
            be saved to ``~/archon_<controller_name>.acf``, or set ``save`` to the path
            of the file to save.
        full
            Whether to read all the configuration lines. If `False`, reads until two
            consecutive empty lines are found.

        """
        key_value_re = re.compile("^(.+?)=(.*)$")

        def parse_line(line):
            k, v = key_value_re.match(line).groups()
            # It seems the GUI replaces / with \ even if that doesn't seem
            # necessary in the INI format.
            k = k.replace("/", "\\")
            if ";" in v or "=" in v or "," in v:
                v = f'"{v}"'
            return k, v

        lines: list[str] = []
        n_blank = 0
        max_lines = 16384
        for n_line in range(max_lines):
            # TODO: It would probably be more efficient to send all the RCONFIG commands
            # at once and then asyncio.gather them. The problem is that in that case we
            # don't know when to stop. Maybe it's faster to get all the lines that way.
            cmd = await self.send_command(f"RCONFIG{n_line:04X}", timeout=0.5)
            if not cmd.succeeded():
                status = cmd.status.name
                raise ArchonError(f"An RCONFIG command returned with code {status!r}")
            if cmd.replies == []:
                raise ArchonError("An RCONFIG command did not return.")
            reply = str(cmd.replies[0])
            if reply == "" and not full:
                n_blank += 1
            else:
                n_blank = 0
                lines.append(reply)

            if n_blank == 2:
                break

        # Trim possible empty lines at the end.
        config = "\n".join(lines).strip().splitlines()
        if not save:
            return config

        # The GUI ACF file includes the system information, so we get it.
        system = await self.get_system()

        c = configparser.ConfigParser()
        c.optionxform = str  # Make it case-sensitive
        c.add_section("SYSTEM")
        for sk, sv in system.items():
            if "_name" in sk.lower():
                continue
            sl = f"{sk.upper()}={sv}"
            k, v = parse_line(sl)
            c.set("SYSTEM", k, v)
        c.add_section("CONFIG")
        for cl in config:
            k, v = parse_line(cl)
            c.set("CONFIG", k, v)

        if isinstance(save, str):
            path = save
        else:
            path = os.path.expanduser(f"~/archon_{self.name}.acf")
        with open(path, "w") as f:
            c.write(f, space_around_delimiters=False)

        return config

    async def write_config(
        self,
        path: str | os.PathLike[str],
        applyall: bool = False,
        poweron: bool = False,
        timeout: float = 0.5,
        notifier: Optional[Callable[[str], None]] = None,
    ):
        """Writes a configuration file to the contoller.

        Parameters
        ----------
        path
            The path to the configuration file to load. It must be in INI format with
            a section called ``[CONFIG]``.
        applyall
            Whether to run ``APPLYALL`` after successfully sending the configuration.
        poweron
            Whether to run ``POWERON`` after successfully sending the configuration.
            Requires ``applyall=True``.
        timeout
            The amount of time to wait for each command to succeed.
        notifier
            A callback that receives a message with the current operation. Useful when
            `.write_config` is called by the actor to report progress to the users.
        """
        notifier = notifier or (lambda x: None)

        notifier("Reading configuration file")

        c = configparser.ConfigParser()
        c.read(path)
        if not c.has_section("CONFIG"):
            raise ArchonError("The config file does not have a CONFIG section.")

        # Undo the INI format: revert \ to / and remove quotes around values.
        config_lines = list(
            map(
                lambda k: k.upper().replace("\\", "/")
                + "="
                + c["CONFIG"][k].strip('"'),
                c["CONFIG"],
            )
        )

        notifier("Clearing previous configuration")
        if not (await self.send_command("CLEARCONFIG", timeout=timeout)).succeeded():
            raise ArchonError("Failed running CLEARCONFIG.")

        notifier("Sending configuration lines")
        n_line = 0
        # TODO: This could benefit from a better handling of the command_id pool, but
        # right now it sends ~1200 lines in <5 seconds.
        for n_chunk in range(len(config_lines) // 100 + 1):
            commands: list[ArchonCommand] = []
            for line in list(config_lines)[100 * n_chunk : 100 * n_chunk + 100]:
                cmd_str = f"WCONFIG{n_line:04X}{line}"
                commands.append(self.send_command(cmd_str, timeout=timeout))
                n_line += 1

            done_commands: tuple[ArchonCommand] = await asyncio.gather(*commands)
            if not all([cmd.succeeded() for cmd in done_commands]):
                for cmd in done_commands:
                    if not cmd.succeeded():
                        raise ArchonError(
                            f"Failed sending line {cmd.raw!r} ({cmd.status.name})"
                        )

        notifier("Sucessfully sent config lines")

        if applyall:
            notifier("Sending APPLYALL")
            cmd = await self.send_command("APPLYALL", timeout=30)
            if not cmd.succeeded():
                raise ArchonError(f"Failed sending APPLYALL ({cmd.status.name})")

        if applyall and poweron:
            notifier("Sending POWERON")
            if not (await self.send_command("POWERON", timeout=timeout)).succeeded():
                raise ArchonError("Failed sending POWERON")

    async def set_param(self, param: str, value: int) -> ArchonCommand:
        """Sets the parameter ``param`` to value ``value`` calling ``FASTLOADPARAM``."""
        cmd = await self.send_command(f"FASTLOADPARAM {param} {value}")
        if not cmd.succeeded():
            raise ArchonError(
                f"Failed setting parameter {param!r} ({cmd.status.name})."
            )
        return cmd

    async def _listen(self):
        """Listens to the reader stream and callbacks on message received."""
        if not self._client:  # pragma: no cover
            raise RuntimeError("Connection is not open.")

        while True:
            # Max length of a reply is 1024 bytes for the message preceded by <xx:
            # We read the first four characters (the maximum length of a complete
            # message: ?xx\n or <xx\n). If the message ends in a newline, we are done;
            # if the message ends with ":", it means what follows are 1024 binary
            # characters without a newline; otherwise, read until the newline which
            # marks the end of this message. In binary, if the response is < 1024
            # bytes, the remaining bytes are filled with NULL (0x00).
            line = await self._client.reader.read(4)

            if line[-1] == ord(b"\n"):
                pass
            elif line[-1] == ord(b":"):
                line += await self._client.reader.read(1024)
            else:
                line += await self._client.reader.readuntil(b"\n")

            self.notify(line)

    def __get_id(self) -> int:
        """Returns an identifier from the pool."""
        if len(self.__id_pool) == 0:
            raise ArchonError("No ids reamining in the pool!")
        return self.__id_pool.pop()

    async def __track_commands(self):
        """Removes complete commands from the list of running commands."""
        while True:
            done_cids = []
            for cid in self.__running_commands.keys():
                if self.__running_commands[cid].done():
                    self.__id_pool.add(cid)
                    done_cids.append(cid)
            for cid in done_cids:
                self.__running_commands.pop(cid)
            await asyncio.sleep(0.5)
