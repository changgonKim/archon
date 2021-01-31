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
from collections.abc import AsyncIterator

from typing import Any, Callable, Iterable, Optional

import numpy
from clu.device import Device

from archon.controller.command import ArchonCommand
from archon.controller.maskbits import ControllerStatus, ModType
from archon.exceptions import ArchonError, ArchonUserWarning

from . import MAX_COMMAND_ID, MAX_CONFIG_LINES

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
    _id_pool = set(range(MAX_COMMAND_ID))

    def __init__(self, host: str, port: int = 4242, name: str = ""):
        self.name = name
        super().__init__(host, port)

        self._status: ControllerStatus = ControllerStatus.UNKNOWN
        self.__status_event = asyncio.Event()

        self._binary_reply: Optional[bytearray] = None

        # TODO: asyncio recommends using asyncio.create_task directly, but that
        # call get_running_loop() which fails in iPython.
        self._job = asyncio.get_event_loop().create_task(self.__track_commands())

    @property
    def status(self) -> ControllerStatus:
        """Returns the status of the controller."""
        return self._status

    @status.setter
    def status(self, value: ControllerStatus):
        """Sets the controller status."""
        self._status = value
        self.__status_event.set()

    async def yield_status(self) -> AsyncIterator[ControllerStatus]:
        """Asynchronous generator yield the status of the controller."""
        yield self.status  # Yield the status on subscription to the generator.
        while True:
            await self.__status_event.wait()
            yield self.status
            self.__status_event.clear()

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
        command_id = command_id or self._get_id()
        if command_id > MAX_COMMAND_ID or command_id < 0:
            raise ArchonError(
                f"Command ID must be in the range [0, {MAX_COMMAND_ID:d}]."
            )

        command = ArchonCommand(
            command_string,
            command_id,
            controller=self,
            **kwargs,
        )
        self.__running_commands[command_id] = command

        self.write(command.raw)

        return command

    async def send_many(
        self,
        cmd_strs: Iterable[str],
        max_chunk=100,
        timeout: Optional[float] = None,
    ) -> tuple[list[ArchonCommand], list[ArchonCommand]]:
        """Sends many commands and waits until they are all done.

        If any command fails or times out, cancels any future command. Returns a list
        of done commands and a list failed commands (empty if all the commands
        have succeeded). Note that ``done+pending`` can be fewer than the length
        of ``cmd_strs``.

        The order in which the commands are sent and done is not guaranteed. If that's
        important, you should use `.send_command`.

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
        # Copy the strings so that we can pop them. Also reverse it because
        # we'll be popping items and we want to conserve the order.
        cmd_strs = list(cmd_strs)[::-1]
        done: list[ArchonCommand] = []

        while len(cmd_strs) > 0:
            pending: list[ArchonCommand] = []
            if len(cmd_strs) < max_chunk:
                max_chunk = len(cmd_strs)
            if len(self._id_pool) >= max_chunk:
                cmd_ids = (self._get_id() for __ in range(max_chunk))
            else:
                cmd_ids = (self._get_id() for __ in range(len(self._id_pool)))
            for cmd_id in cmd_ids:
                cmd_str = cmd_strs.pop()
                cmd = self.send_command(cmd_str, command_id=cmd_id, timeout=timeout)
                pending.append(cmd)
            done_cmds: tuple[ArchonCommand] = await asyncio.gather(*pending)
            if all([cmd.succeeded() for cmd in done_cmds]):
                done += done_cmds
                for cmd in done_cmds:
                    self._id_pool.add(cmd.command_id)
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

    async def read_config(self, save: str | bool = False) -> list[str]:
        """Reads the configuration from the controller.

        Parameters
        ----------
        save
            Save the configuration to a file. If ``save=True``, the configuration will
            be saved to ``~/archon_<controller_name>.acf``, or set ``save`` to the path
            of the file to save.
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

        cmd_strs = [f"RCONFIG{n_line:04X}" for n_line in range(MAX_CONFIG_LINES)]
        done, failed = await self.send_many(cmd_strs, max_chunk=200, timeout=0.5)
        if len(failed) > 0:
            ff = failed[0]
            status = ff.status.name
            raise ArchonError(f"An RCONFIG command returned with code {status!r}")

        if any([len(cmd.replies) != 1 for cmd in done]):
            raise ArchonError("Some commands did not get any reply.")

        lines = [str(cmd.replies[0]) for cmd in done]

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
        timeout: float = 1,
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

        if not os.path.exists(path):
            raise ArchonError(f"File {path} does not exist.")

        c = configparser.ConfigParser()
        c.read(path)
        if not c.has_section("CONFIG"):
            raise ArchonError("The config file does not have a CONFIG section.")

        # Undo the INI format: revert \ to / and remove quotes around values.
        config = c["CONFIG"]
        lines = list(
            map(
                lambda k: k.upper().replace("\\", "/") + "=" + config[k].strip('"'),
                config,
            )
        )

        notifier("Clearing previous configuration")
        if not (await self.send_command("CLEARCONFIG", timeout=timeout)).succeeded():
            self.status = ControllerStatus.ERROR
            raise ArchonError("Failed running CLEARCONFIG.")

        notifier("Sending configuration lines")

        cmd_strs = [f"WCONFIG{n_line:04X}{line}" for n_line, line in enumerate(lines)]
        done, failed = await self.send_many(cmd_strs, max_chunk=200, timeout=timeout)
        if len(failed) > 0:
            ff = failed[0]
            self.status = ControllerStatus.ERROR
            raise ArchonError(f"Failed sending line {ff.raw!r} ({ff.status.name})")

        notifier("Sucessfully sent config lines")

        if applyall:
            notifier("Sending APPLYALL")
            cmd = await self.send_command("APPLYALL", timeout=5)
            if not cmd.succeeded():
                self.status = ControllerStatus.ERROR
                raise ArchonError(f"Failed sending APPLYALL ({cmd.status.name})")

            if poweron:
                notifier("Sending POWERON")
                cmd = await self.send_command("POWERON", timeout=timeout)
                if not cmd.succeeded():
                    self.status = ControllerStatus.ERROR
                    raise ArchonError(f"Failed sending POWERON ({cmd.status.name})")

        self.status = ControllerStatus.IDLE

    async def reset(self):
        """Cancels exposures and resets timing."""
        await self.set_param("ContinuousExposures", 0)
        await self.set_param("Exposures", 0)
        cmd = await self.send_command("RESETTIMING", timeout=1)
        if not cmd.succeeded():
            self.status = ControllerStatus.ERROR
            raise ArchonError(f"Failed sending RESETTIMING ({cmd.status.name})")

        # TODO: here we should do some more checks before we say it's IDLE.
        self.status = ControllerStatus.IDLE

    async def set_param(self, param: str, value: int) -> ArchonCommand:
        """Sets the parameter ``param`` to value ``value`` calling ``FASTLOADPARAM``."""
        cmd = await self.send_command(f"FASTLOADPARAM {param} {value}")
        if not cmd.succeeded():
            raise ArchonError(
                f"Failed setting parameter {param!r} ({cmd.status.name})."
            )
        return cmd

    async def integrate(self, exposure_time=1):
        """Integrates the CCD for ``exposure_time`` seconds.

        Returns immediately once the exposure has begun.
        """
        if not self.status == ControllerStatus.IDLE:
            raise ArchonError("Status must be IDLE to start integrating.")

        await self.set_param("IntMS", int(exposure_time * 1000))
        await self.set_param("Exposures", 1)

        self.status = ControllerStatus.EXPOSING

    async def fetch(
        self,
        buffer_no: int = -1,
        notifier: Optional[Callable[[str], None]] = None,
    ) -> numpy.ndarray:
        """Fetches a frame buffer and returns a Numpy array.

        Parameters
        ----------
        buffer_no
            The frame buffer number to read. Use ``-1`` to read the most recently
            complete frame.
        notifier
            A callback that receives a message with the current operation. Useful when
            `.fetch` is called by the actor to report progress to the users.
        """
        notifier = notifier or (lambda x: None)
        frame_info = await self.get_frame()

        if buffer_no not in [1, 2, 3, -1]:
            raise ArchonError(f"Invalid frame buffer {buffer_no}.")

        if buffer_no == -1:
            buffers = [
                (n, frame_info[f"buf{n}timestamp"])
                for n in [1, 2, 3]
                if frame_info[f"buf{n}complete"] == 1
            ]
            if len(buffers) == 0:
                raise ArchonError("There are no buffers ready to be read.")
            sorted_buffers = sorted(buffers, key=lambda x: x[1], reverse=True)
            buffer_no = sorted_buffers[0][0]
        else:
            if frame_info[f"buf{buffer_no}complete"] == 0:
                raise ArchonError(f"Buffer frame {buffer_no} cannot be read.")

        self.status = ControllerStatus.FETCHING

        # Lock for reading
        notifier(f"Locking buffer {buffer_no}")
        await self.send_command(f"LOCK{buffer_no}")

        width = frame_info[f"buf{buffer_no}width"]
        height = frame_info[f"buf{buffer_no}height"]
        bytes_per_pixel = 2 if frame_info[f"buf{buffer_no}sample"] == 0 else 4
        n_bytes = width * height * bytes_per_pixel
        n_blocks: int = int(numpy.ceil(n_bytes / 1024.0))  # type: ignore

        start_address = frame_info[f"buf{buffer_no}base"]

        notifier("Reading frame buffer ...")

        # Set the expected length of binary buffer to read, including the prefixes.
        self.set_binary_reply_size((1024 + 4) * n_blocks)

        cmd: ArchonCommand = await self.send_command(
            f"FETCH{start_address:08X}{n_blocks:08X}",
            timeout=None,
        )

        # Unlock all
        notifier("Frame buffer readout complete. Unlocking all buffers.")
        await self.send_command("LOCK0")

        # The full read buffer probably contains some extra bytes to complete the 1024
        # reply. We get only the bytes we know are part of the buffer.
        frame = cmd.replies[0].reply[0:n_bytes]

        # Convert to uint16 array and reshape.
        dtype = f"<u{bytes_per_pixel}"  # Buffer is little-endian
        arr = numpy.frombuffer(frame, dtype=dtype)
        arr = arr.reshape(height, width)

        self.status = ControllerStatus.IDLE

        return arr

    def set_binary_reply_size(self, size: int):
        """Sets the size of the binary buffers."""
        self._binary_reply = bytearray(size)

    async def _listen(self):
        """Listens to the reader stream and callbacks on message received."""
        if not self._client:  # pragma: no cover
            raise RuntimeError("Connection is not open.")

        n_binary = 0
        while True:
            # Max length of a reply is 1024 bytes for the message preceded by <xx:
            # We read the first four characters (the maximum length of a complete
            # message: ?xx\n or <xx\n). If the message ends in a newline, we are done;
            # if the message ends with ":", it means what follows are 1024 binary
            # characters without a newline; otherwise, read until the newline which
            # marks the end of this message. In binary, if the response is < 1024
            # bytes, the remaining bytes are filled with NULL (0x00).
            line = await self._client.reader.readexactly(4)
            if line[-1] == ord(b"\n"):
                pass
            elif line[-1] == ord(b":"):
                line += await self._client.reader.readexactly(1024)
                # If we know the length of the binary reply to expect, we set that
                # slice of the bytearray and continue. We wait until all the buffer
                # has been read before sending the notification. This is significantly
                # more efficient because we don't create an ArchonCommandReply for each
                # chunk of the binary reply. It is, however, necessary to know the
                # exact size of the reply because there is nothing that we can parse
                # to know a reply is the last one. Also, we don't want to keep appending
                # to a bytes string. We need to allocate all the memory first with
                # a bytearray or it's very inefficient.
                #
                # NOTE: this assumes that once the binary reply begins, no no other
                # reply is going to arrive in the middle of it. I think that's unlikely,
                # and probably prevented by the controller, but it's worth keeping in
                # mind.
                #
                if self._binary_reply:
                    self._binary_reply[n_binary : n_binary + 1028] = line
                    n_binary += 1028  # How many bytes of the binary reply have we read.
                    if n_binary == len(self._binary_reply):
                        # This was the last chunk. Set line to the full reply and
                        # reset the binary reply and counter.
                        line = self._binary_reply
                        self._binary_reply = None
                        n_binary = 0
                    else:
                        # Skip notifying because the binary reply is still incomplete.
                        continue
            else:
                line += await self._client.reader.readuntil(b"\n")

            self.notify(line)

    def _get_id(self) -> int:
        """Returns an identifier from the pool."""
        if len(self._id_pool) == 0:
            raise ArchonError("No ids reamining in the pool!")
        return self._id_pool.pop()

    async def __track_commands(self):
        """Removes complete commands from the list of running commands."""
        while True:
            done_cids = []
            for cid in self.__running_commands.keys():
                if self.__running_commands[cid].done():
                    self._id_pool.add(cid)
                    done_cids.append(cid)
            for cid in done_cids:
                self.__running_commands.pop(cid)
            await asyncio.sleep(0.5)
