# License: MIT
# Copyright © 2023 Frequenz Energy-as-a-Service GmbH

"""The power manager."""

from __future__ import annotations

import asyncio
import logging
import typing

from frequenz.channels import Receiver, Sender
from frequenz.channels.util import select, selected_from
from typing_extensions import override

from .._actor import Actor
from .._channel_registry import ChannelRegistry
from ._base_classes import Algorithm, BaseAlgorithm, Proposal, Report, ReportRequest
from ._matryoshka import Matryoshka

_logger = logging.getLogger(__name__)

if typing.TYPE_CHECKING:
    from ...timeseries.battery_pool import PowerMetrics
    from .. import power_distributing


class PowerManagingActor(Actor):
    """The power manager."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        proposals_receiver: Receiver[Proposal],
        bounds_subscription_receiver: Receiver[ReportRequest],
        power_distributing_requests_sender: Sender[power_distributing.Request],
        channel_registry: ChannelRegistry,
        algorithm: Algorithm = Algorithm.MATRYOSHKA,
    ):
        """Create a new instance of the power manager.

        Args:
            proposals_receiver: The receiver for proposals.
            bounds_subscription_receiver: The receiver for bounds subscriptions.
            power_distributing_requests_sender: The sender for power distribution
                requests.
            channel_registry: The channel registry.
            algorithm: The power management algorithm to use.


        Raises:
            NotImplementedError: When an unknown algorithm is given.
        """
        if algorithm is not Algorithm.MATRYOSHKA:
            raise NotImplementedError(
                f"PowerManagingActor: Unknown algorithm: {algorithm}"
            )

        self._bounds_subscription_receiver = bounds_subscription_receiver
        self._power_distributing_requests_sender = power_distributing_requests_sender
        self._channel_registry = channel_registry
        self._proposals_receiver = proposals_receiver

        self._system_bounds: dict[frozenset[int], PowerMetrics] = {}
        self._bound_tracker_tasks: dict[frozenset[int], asyncio.Task[None]] = {}
        self._subscriptions: dict[frozenset[int], dict[int, Sender[Report]]] = {}

        self._algorithm: BaseAlgorithm = Matryoshka()

        super().__init__()

    async def _send_report(self, battery_ids: frozenset[int]) -> None:
        """Send a report for a set of batteries.

        Args:
            battery_ids: The battery IDs.
        """
        bounds = self._system_bounds.get(battery_ids)
        if bounds is None:
            _logger.warning("PowerManagingActor: No bounds for %s", battery_ids)
            return
        for priority, sender in self._subscriptions.get(battery_ids, {}).items():
            await sender.send(self._algorithm.get_status(battery_ids, priority, bounds))

    async def _bounds_tracker(
        self,
        battery_ids: frozenset[int],
        bounds_receiver: Receiver[PowerMetrics],
    ) -> None:
        """Track the power bounds of a set of batteries and update the cache.

        Args:
            battery_ids: The battery IDs.
            bounds_receiver: The receiver for power bounds.
        """
        async for bounds in bounds_receiver:
            self._system_bounds[battery_ids] = bounds
            await self._send_report(battery_ids)

    async def _add_bounds_tracker(self, battery_ids: frozenset[int]) -> None:
        """Add a bounds tracker.

        Args:
            battery_ids: The battery IDs.
        """
        # Pylint assumes that this import is cyclic, but it's not.
        from ... import (  # pylint: disable=import-outside-toplevel,cyclic-import
            microgrid,
        )

        battery_pool = microgrid.battery_pool(battery_ids)
        # pylint: disable=protected-access
        bounds_receiver = battery_pool._system_power_bounds.new_receiver()
        # pylint: enable=protected-access

        # Fetch the latest system bounds once, before starting the bounds tracker task,
        # so that when this function returns, there's already some bounds available.
        self._system_bounds[battery_ids] = await bounds_receiver.receive()

        # Start the bounds tracker, for ongoing updates.
        self._bound_tracker_tasks[battery_ids] = asyncio.create_task(
            self._bounds_tracker(battery_ids, bounds_receiver)
        )

    @override
    async def _run(self) -> None:
        """Run the power managing actor."""
        from .. import power_distributing  # pylint: disable=import-outside-toplevel

        async for selected in select(
            self._proposals_receiver, self._bounds_subscription_receiver
        ):
            if selected_from(selected, self._proposals_receiver):
                proposal = selected.value
                if proposal.battery_ids not in self._system_bounds:
                    await self._add_bounds_tracker(proposal.battery_ids)

                target_power = self._algorithm.handle_proposal(
                    proposal, self._system_bounds[proposal.battery_ids]
                )

                await self._power_distributing_requests_sender.send(
                    power_distributing.Request(
                        power=target_power,
                        batteries=proposal.battery_ids,
                        request_timeout=proposal.request_timeout,
                        adjust_power=True,
                        include_broken_batteries=proposal.include_broken_batteries,
                    )
                )

            elif selected_from(selected, self._bounds_subscription_receiver):
                sub = selected.value
                battery_ids = sub.battery_ids
                priority = sub.priority

                if battery_ids not in self._subscriptions:
                    self._subscriptions[battery_ids] = {
                        priority: self._channel_registry.new_sender(
                            sub.get_channel_name()
                        )
                    }
                elif priority not in self._subscriptions[battery_ids]:
                    self._subscriptions[battery_ids][
                        priority
                    ] = self._channel_registry.new_sender(sub.get_channel_name())

                if sub.battery_ids not in self._bound_tracker_tasks:
                    await self._add_bounds_tracker(sub.battery_ids)

                await self._send_report(battery_ids)
