# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2024 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This package contains round behaviours of LearningAbciApp."""

import json
from abc import ABC
from typing import Generator, Set, Type, cast
from hexbytes import HexBytes

from packages.valory.contracts.erc20.contract import ERC20
from packages.valory.contracts.gnosis_safe.contract import (
    GnosisSafeContract,
    SafeOperation,
)
from packages.valory.contracts.multisend.contract import (
    MultiSendContract,
    MultiSendOperation,
)
from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.skills.transaction_settlement_abci.payload_tools import (
    hash_payload_to_hex,
)

from packages.valory.skills.abstract_round_abci.base import AbstractRound
from packages.valory.skills.abstract_round_abci.behaviours import (
    AbstractRoundBehaviour,
    BaseBehaviour,
)
from packages.valory.skills.learning_abci.models import Params, SharedState
from packages.valory.skills.learning_abci.payloads import (
    APICheckPayload,
    DecisionMakingPayload,
    TxPreparationPayload,
)
from packages.valory.skills.learning_abci.rounds import (
    APICheckRound,
    DecisionMakingRound,
    Event,
    LearningAbciApp,
    SynchronizedData,
    TxPreparationRound,
)


HTTP_OK = 200
GNOSIS_CHAIN_ID = "gnosis"
TX_DATA = b"0x"
SAFE_GAS = 0
VALUE_KEY = "value"
TO_ADDRESS_KEY = "to_address"
ETHER_VALUE = 10**18


class LearningBaseBehaviour(BaseBehaviour, ABC):  # pylint: disable=too-many-ancestors
    """Base behaviour for the learning_abci skill."""

    @property
    def synchronized_data(self) -> SynchronizedData:
        """Return the synchronized data."""
        return cast(SynchronizedData, super().synchronized_data)

    @property
    def params(self) -> Params:
        """Return the params."""
        return cast(Params, super().params)

    @property
    def local_state(self) -> SharedState:
        """Return the state."""
        return cast(SharedState, self.context.state)


class APICheckBehaviour(LearningBaseBehaviour):  # pylint: disable=too-many-ancestors
    """APICheckBehaviour"""

    matching_round: Type[AbstractRound] = APICheckRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            sender = self.context.agent_address
            price = yield from self.get_price()
            balance = yield from self.get_balance()
            payload = APICheckPayload(sender=sender, price=price, balance=balance)

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_price(self):
        """Get token price from Coingecko"""
        # Interact with Coingecko's API
        request_template = self.params.coingecko_price_template
        token = "starknet"
        # replace token name
        url = request_template.replace("autonolas", token)
        # replace placeholder of {api_key} with the env value
        url = url.replace("{api_key}", self.params.coingecko_api_key)

        result = yield from self.get_http_response(method="GET", url=url)

        if result.status_code != 200:
            self.context.logger.error(
                f"Error while fetching coingecko API. Status Code: {result.status_code}, Status Text: {result.status_text}"
            )
            return None

        result_str = result.body.decode("utf-8")
        data = json.loads(result_str)
        price = data[token]["usd"]
        self.context.logger.info(f"Price is {price}")
        return price

    def get_balance(self):
        """Get balance"""
        # Use the contract api to interact with the ERC20 contract
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.params.token_contract_address,
            contract_id=str(ERC20.contract_id),
            contract_callable="check_balance",
            account=self.synchronized_data.safe_contract_address,
            chain_id=GNOSIS_CHAIN_ID,
        )

        if response_msg.performative != ContractApiMessage.Performative.RAW_TRANSACTION:
            self.context.logger.error(
                f"Could not calculate the balance of the safe: {response_msg}"
            )
            return False

        # fetching wallet balance of xDAI, token decimal is 18
        native_balance = (
            response_msg.raw_transaction.body.get("wallet", None)
        ) / 10**18
        # fetching token balance of wBTC, token decimal is 8
        token_balance = (response_msg.raw_transaction.body.get("token", None)) / 10**8

        self.context.logger.info(
            f"xDAI Balance is {native_balance}, wBTC balance is {token_balance}"
        )

        return token_balance


class DecisionMakingBehaviour(
    LearningBaseBehaviour
):  # pylint: disable=too-many-ancestors
    """DecisionMakingBehaviour"""

    matching_round: Type[AbstractRound] = DecisionMakingRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            sender = self.context.agent_address
            event = self.get_event()
            payload = DecisionMakingPayload(sender=sender, event=event)

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_event(self):
        """Get the next event"""
        # Using the token price from the previous round, decide whether we should make a transfer or not
        # using dummy decision making condition
        if self.synchronized_data.price < 0.5:
            event = Event.DONE.value
            self.context.logger.info(f"Threshold not reached, moving to {event}")
        else:
            event = Event.TRANSACT.value
            self.context.logger.info(f"Threshold reached, moving to {event}")

        self.context.logger.info(f"Event is {event}")
        return event


class TxPreparationBehaviour(
    LearningBaseBehaviour
):  # pylint: disable=too-many-ancestors
    """TxPreparationBehaviour"""

    matching_round: Type[AbstractRound] = TxPreparationRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            multi_send_txs = []

            sender = self.context.agent_address
            native_transfer_data = yield from self.get_native_transfer_tx_data()
            multi_send_txs.append(native_transfer_data)
            token_transfer_data = yield from self.get_token_transfer_tx_data()
            multi_send_txs.append(token_transfer_data)

            self.context.logger.info(f"multisend txs: {multi_send_txs}")

            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
                contract_address=self.params.multisend_contract_address,
                contract_id=str(MultiSendContract.contract_id),
                contract_callable="get_tx_data",
                multi_send_txs=multi_send_txs,
                chain_id=GNOSIS_CHAIN_ID,
            )

            if (
                contract_api_msg.performative
                != ContractApiMessage.Performative.RAW_TRANSACTION
            ):
                self.context.logger.error(
                    f"Could not get Multisend tx hash. "
                    f"Expected: {ContractApiMessage.Performative.RAW_TRANSACTION.value}, "
                    f"Actual: {contract_api_msg.performative.value}"
                )
                return None

            multisend_data = cast(str, contract_api_msg.raw_transaction.body["data"])
            multisend_data = multisend_data[2:]
            self.context.logger.info(f"Multisend data: {multisend_data}")

            contract_api_msg = yield from self.get_contract_api_response(
                performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
                contract_address=self.synchronized_data.safe_contract_address,
                contract_id=str(GnosisSafeContract.contract_id),
                contract_callable="get_raw_safe_transaction_hash",
                to_address=self.params.multisend_contract_address,
                value=sum(tx["value"] for tx in multi_send_txs),
                data=bytes.fromhex(multisend_data),
                operation=SafeOperation.DELEGATE_CALL.value,
                safe_tx_gas=SAFE_GAS,
                chain_id=GNOSIS_CHAIN_ID,
            )

            if contract_api_msg.performative != ContractApiMessage.Performative.STATE:
                self.context.logger.error(
                    f"Could not get Multisend Gnosis Safe tx hash. "
                    f"Expected: {ContractApiMessage.Performative.STATE.value}, "
                    f"Actual: {contract_api_msg.performative.value}"
                )
                return None

            safe_tx_hash = cast(str, contract_api_msg.state.body["tx_hash"])
            safe_tx_hash = safe_tx_hash[2:]
            self.context.logger.info(f"Hash of the Safe transaction: {safe_tx_hash}")

            tx_hash_payload = hash_payload_to_hex(
                safe_tx_hash=safe_tx_hash,
                ether_value=sum(tx["value"] for tx in multi_send_txs),
                safe_tx_gas=SAFE_GAS,
                to_address=self.params.multisend_contract_address,
                data=bytes.fromhex(multisend_data),
                operation=SafeOperation.DELEGATE_CALL.value,
            )
            self.context.logger.info(f"Final tx payload is: {tx_hash_payload}")
            payload = TxPreparationPayload(
                sender=sender, tx_submitter=None, tx_hash=tx_hash_payload
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

        # Sample transaction
        # https://dashboard.tenderly.co/explorer/vnet/c0c35487-a16b-44e8-baed-5a93cda21761/tx/0x119d12467e2769d6e632056750c307af9fa79211689a4dc737b4e610dc491d69

    def get_native_transfer_tx_data(self):
        """Get the tx data"""
        self.context.logger.info(f"Inside function call: Native transfer")

        # We need to prepare a 10**18 wei transfer from the safe to another (configurable) account.
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.synchronized_data.safe_contract_address,
            contract_id=str(GnosisSafeContract.contract_id),
            contract_callable="get_raw_safe_transaction_hash",
            to_address=self.params.transfer_target_address,
            value=ETHER_VALUE,
            data=TX_DATA,
            safe_tx_gas=SAFE_GAS,
            chain_id=GNOSIS_CHAIN_ID,
        )

        if response_msg.performative != ContractApiMessage.Performative.RAW_TRANSACTION:
            self.context.logger.error(
                f"Could not get native transfer hash. "
                f"Expected: {ContractApiMessage.Performative.RAW_TRANSACTION.value}, "
                f"Actual: {response_msg.performative.value}"
            )
            return None

        self.context.logger.info(f"Native transfer response msg is {response_msg}")

        tx_hash_data = cast(str, response_msg.raw_transaction.body["tx_hash"])
        self.context.logger.info(f"Transaction hash data is {tx_hash_data}")
        self.context.logger.info(
            f"Native transfer Transaction hash data is {tx_hash_data}"
        )
        return {
            "operation": MultiSendOperation.CALL,
            "to": self.params.transfer_target_address,
            "value": ETHER_VALUE,
            "data": tx_hash_data,
        }

    def get_token_transfer_tx_data(self):
        """Get the tx data"""
        self.context.logger.info(f"Inside function call: Token transfer")

        # We need to prepare a 1 token transfer from the safe to another (configurable) account.
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.params.token_contract_address,
            contract_id=str(ERC20.contract_id),
            contract_callable="build_transfer_tx",
            receiver=self.params.transfer_target_address,
            amount=10**8,
            chain_id=GNOSIS_CHAIN_ID,
        )

        self.context.logger.info(f"Token transfer response msg is {response_msg}")

        if response_msg.performative != ContractApiMessage.Performative.RAW_TRANSACTION:
            self.context.logger.error(
                f"Could not get token transfer hash. "
                f"Expected: {ContractApiMessage.Performative.RAW_TRANSACTION.value}, "
                f"Actual: {response_msg.performative.value}"
            )
            return None

        tx_hash_data = HexBytes(
            cast(bytes, response_msg.raw_transaction.body["data"]).hex()
        )
        self.context.logger.info(
            f"Token transfer Transaction hash data is {tx_hash_data}"
        )
        return {
            "operation": MultiSendOperation.CALL,
            "to": self.params.token_contract_address,
            "value": 0,
            "data": tx_hash_data,
        }


class LearningRoundBehaviour(AbstractRoundBehaviour):
    """LearningRoundBehaviour"""

    initial_behaviour_cls = APICheckBehaviour
    abci_app_cls = LearningAbciApp  # type: ignore
    behaviours: Set[Type[BaseBehaviour]] = [  # type: ignore
        APICheckBehaviour,
        DecisionMakingBehaviour,
        TxPreparationBehaviour,
    ]
