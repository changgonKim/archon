#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2021-01-23
# @Filename: test_get_status.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

import pytest

from archon.controller.controller import ArchonController
from archon.exceptions import ArchonError

pytestmark = [pytest.mark.asyncio]


@pytest.mark.commands([["STATUS", ["<{cid}KEY1=1 KEY2=-2.1"]]])
async def test_get_status(controller: ArchonController):
    status = await controller.get_status()
    assert isinstance(status, dict)
    assert len(status) == 2
    assert status["key1"] == 1
    assert status["key2"] == -2.1


@pytest.mark.commands([["STATUS", ["?{cid}"]]])
async def test_get_status_error(controller: ArchonController):
    with pytest.raises(ArchonError):
        await controller.get_status()
