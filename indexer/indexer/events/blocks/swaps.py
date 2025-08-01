from __future__ import annotations

import logging

from pytoniq_core import Cell, Slice, begin_cell

from indexer.core.database import Message
from indexer.events import context
from indexer.events.blocks.basic_blocks import CallContractBlock
from indexer.events.blocks.basic_matchers import (
    BlockMatcher,
    BlockTypeMatcher,
    ContractMatcher,
    OrMatcher,
    RecursiveMatcher,
    child_sequence_matcher,
)
from indexer.events.blocks.core import Block, SingleLevelWrapper
from indexer.events.blocks.jettons import JettonTransferBlock, PTonTransferMatcher
from indexer.events.blocks.labels import LabelBlock, labeled
from indexer.events.blocks.messages import (
    DedustPayout,
    DedustPayoutFromPool,
    DedustSwap,
    DedustSwapExternal,
    DedustSwapNotification,
    DedustSwapPayload,
    DedustSwapPeer,
    JettonNotify,
    PTonTransfer,
    StonfiPaymentRequest,
    StonfiSwapMessage,
    StonfiSwapV2,
    StonfiV2PayTo,
    ToncoPoolV3Swap,
    ToncoPoolV3SwapPayload,
    ToncoRouterV3PayTo,
)
from indexer.events.blocks.messages.coffee import (
    CoffeeNotification,
    CoffeePayout,
    CoffeePayoutInternal,
    CoffeeSwapInternal,
    CoffeeSwapNative,
    CoffeeSwapSuccessfulEvent,
)
from indexer.events.blocks.messages.jettons import JettonNotify, JettonTransfer
from indexer.events.blocks.utils import AccountId, Amount, Asset
from indexer.events.blocks.utils.address_selectors import (
    extract_target_wallet_stonfi_v2_swap,
)
from indexer.events.blocks.utils.block_utils import (
    find_call_contract,
    find_call_contracts,
    find_messages,
    get_labeled,
)


logger = logging.getLogger(__name__)

stonfi_swap_ok_exit_code = 0xc64370e5
stonfi_swap_ok_ref_exit_code = 0x45078540
stonfi_swap_no_liq_exit_code = 0x5ffe1295
stonfi_swap_reserve_err_exit_code = 0x38976e9b
stonfi_sender_related_exit_codes = [
    stonfi_swap_reserve_err_exit_code,
    stonfi_swap_no_liq_exit_code,
    stonfi_swap_ok_exit_code
]

class JettonSwapBlock(Block):
    def __init__(self, data):
        super().__init__('jetton_swap', [], data)

    def __repr__(self):
        return f"jetton_swap {self.data}"


async def _get_block_data(block, other_blocks):
    swap_call_block = next(x for x in other_blocks if isinstance(x, CallContractBlock) and
                           x.opcode == StonfiSwapMessage.opcode)
    swap_message = StonfiSwapMessage(swap_call_block.get_body())
    payment_requests_messages = [(StonfiPaymentRequest(x.get_body()), x) for x in
                                 find_call_contracts(other_blocks, StonfiPaymentRequest.opcode)]
    assert len(payment_requests_messages) > 0
    if len(payment_requests_messages) > 2:
        print("Multiple payment requests found ", swap_call_block.event_nodes[0].message.trace_id)

    out_amt = None
    out_addr = None
    ref_amt = None
    ref_addr = None
    outgoing_jetton_transfer = None
    in_jetton_transfer = swap_call_block.previous_block
    success_swap = False
    # Find payment request and outgoing jetton transfer
    for payment_request, payment_request_block in payment_requests_messages:
        if payment_request.amount0_out > 0:
            amount = payment_request.amount0_out
            addr = payment_request.token0_out
        else:
            amount = payment_request.amount1_out
            addr = payment_request.token1_out
        if payment_request.exit_code in stonfi_sender_related_exit_codes:
            success_swap = (payment_request.exit_code == stonfi_swap_ok_exit_code)
            if out_amt is None:
                outgoing_jetton_transfer = next(b for b in payment_request_block.next_blocks
                                                if isinstance(b, JettonTransferBlock))
                out_amt = amount
                out_addr = addr
            elif out_amt < amount:
                outgoing_jetton_transfer = next(b for b in payment_request_block.next_blocks
                                                if isinstance(b, JettonTransferBlock))
                ref_amt = out_amt
                ref_addr = out_addr
                out_amt = amount
                out_addr = addr
        elif payment_request.exit_code == stonfi_swap_ok_ref_exit_code:
            ref_amt = amount
            ref_addr = addr
            for b in payment_request_block.bfs_iter():
                if b in other_blocks:
                    other_blocks.remove(b)

    actual_out_addr = out_addr
    if isinstance(block, JettonTransferBlock) and block.jetton_transfer_message.stonfi_swap_body is not None:
        out_addr = block.jetton_transfer_message.stonfi_swap_body['jetton_wallet']
    out_wallet = await context.interface_repository.get().get_jetton_wallet(out_addr.to_str(False).upper())
    actual_out_wallet = await context.interface_repository.get().get_jetton_wallet(actual_out_addr.to_str(False).upper())
    dex_in_wallet = await context.interface_repository.get().get_jetton_wallet(
        swap_message.token_wallet.to_str(False).upper())
    actual_out_jetton = AccountId(actual_out_wallet.jetton) if actual_out_wallet is not None else None
    out_jetton = AccountId(out_wallet.jetton) if out_wallet is not None else None
    in_jetton = AccountId(dex_in_wallet.jetton) if dex_in_wallet is not None else None

    in_source_jetton_wallet = None
    if in_jetton_transfer.data['has_internal_transfer']:
        in_source_jetton_wallet = in_jetton_transfer.data['sender_wallet']

    out_destination_jetton_wallet = None
    if outgoing_jetton_transfer.data['has_internal_transfer']:
        out_destination_jetton_wallet = outgoing_jetton_transfer.data['receiver_wallet']

    incoming_transfer = {
        'asset': Asset(is_ton=in_jetton is None, jetton_address=in_jetton),
        'amount': Amount(swap_message.amount),
        'source': AccountId(swap_message.from_real_user),
        'source_jetton_wallet': in_source_jetton_wallet,
        'destination': AccountId(dex_in_wallet.owner),
        'destination_jetton_wallet': AccountId(swap_message.token_wallet),
    }

    outgoing_transfer = {
        'asset': Asset(is_ton=actual_out_jetton is None, jetton_address=actual_out_jetton),
        'amount': Amount(out_amt),
        'source': outgoing_jetton_transfer.data['sender'],
        'source_jetton_wallet': outgoing_jetton_transfer.data['sender_wallet']
    }
    if out_destination_jetton_wallet is not None:
        outgoing_transfer['destination_jetton_wallet'] = out_destination_jetton_wallet
        outgoing_transfer['destination'] = outgoing_jetton_transfer.data['receiver']
    elif in_jetton_transfer.data['stonfi_swap_body'] is not None:
        outgoing_transfer['destination'] = AccountId(in_jetton_transfer.data['stonfi_swap_body']['user_address'])
        outgoing_transfer['destination_jetton_wallet'] = None
    else:
        outgoing_transfer['destination'] = AccountId(swap_message.from_user_address)
        outgoing_transfer['destination_jetton_wallet'] = None
    if out_jetton:
        target_asset = Asset(is_ton=out_jetton is None, jetton_address=out_jetton)
    else:
        target_asset = None
    return {
        'dex': 'stonfi',
        'sender': AccountId(swap_message.from_real_user),
        'receiver': AccountId(swap_message.from_user_address),
        'dex_incoming_transfer': incoming_transfer,
        'dex_outgoing_transfer': outgoing_transfer,
        'destination_asset': target_asset,
        'destination_wallet': AccountId(out_addr) if out_addr is not None else None,
        'referral_amount': Amount(ref_amt),
        'referral_address': AccountId(ref_addr) if ref_addr is not None else None,
        'peer_swaps': [],
        'success': success_swap
    }


class StonfiSwapBlockMatcher(BlockMatcher):
    def __init__(self):
        super().__init__(parent_matcher=None, optional=False,
                         child_matcher=child_sequence_matcher([
                             ContractMatcher(opcode=StonfiSwapMessage.opcode),
                             ContractMatcher(opcode=StonfiPaymentRequest.opcode),
                             BlockTypeMatcher(block_type='jetton_transfer')
                         ]))

    def test_self(self, block: Block):
        return isinstance(block, JettonTransferBlock)

    async def build_block(self, block: Block, other_blocks: list[Block]) -> list[Block]:
        data = await _get_block_data(block, other_blocks)
        new_block = JettonSwapBlock(data)
        if not data['success']:
            new_block.failed = True
        include = [block]
        include.extend(other_blocks)
        new_block.merge_blocks(include)
        return [new_block]


class DedustPeerBlockMatcher(BlockMatcher):

    def __init__(self):
        super().__init__(parent_matcher=None, optional=False, child_matcher=None,
                         children_matchers=[ContractMatcher(opcode=DedustSwapNotification.opcode),
                                            ContractMatcher(opcode=DedustPayoutFromPool.opcode,
                                                            child_matcher=OrMatcher([
                                                                ContractMatcher(opcode=DedustPayout.opcode),
                                                                BlockTypeMatcher(block_type='jetton_transfer')
                                                            ]),
                                                            optional=True)])

    def test_self(self, block: Block):
        return isinstance(block, CallContractBlock) and block.opcode == DedustSwapPeer.opcode

    async def build_block(self, block: Block, other_blocks: list[Block]) -> list[Block]:
        wrapper = SingleLevelWrapper()
        wrapper.wrap([block] + other_blocks)
        return [wrapper]


class StonfiV2SwapBlockMatcher(BlockMatcher):
    swap_opcode = 0x6664de2a
    pay_to_opcode = 0x657b54f5
    pay_vault_opcode = 0x63381632
    deposit_ref_fee_opcode = 0x0490f09b

    def __init__(self):

        payout_matcher = labeled('payout', ContractMatcher(self.pay_to_opcode, child_matcher=None))

        peer_swap_matcher = labeled('peer_swap', ContractMatcher(self.swap_opcode,
                                                                 child_matcher=payout_matcher,
                                                                 optional=True))

        payout_matcher.child_matcher = OrMatcher([
            labeled('out_transfer', BlockTypeMatcher(block_type='jetton_transfer',
                                                     child_matcher=peer_swap_matcher,
                                                     optional=True)),
            peer_swap_matcher])

        in_pton_transfer = ContractMatcher(opcode=JettonNotify.opcode,
                                           parent_matcher=labeled('in_transfer',
                                                                  ContractMatcher(opcode=0x01f3835d)))

        in_transfer = OrMatcher([labeled('in_transfer', BlockTypeMatcher(block_type='jetton_transfer')),
                                 in_pton_transfer])

        super().__init__(parent_matcher=in_transfer, optional=False,
                         child_matcher=payout_matcher)

    def test_self(self, block: Block):
        return isinstance(block, CallContractBlock) and block.opcode == self.swap_opcode

    async def _get_target_asset_from_notification(self, message: Message):
        try:
            address = next(iter(extract_target_wallet_stonfi_v2_swap(message)), None)
            if address is None:
                return None
            jetton_wallet = await context.interface_repository.get().get_jetton_wallet(address)
            if jetton_wallet is not None:
                return Asset(is_ton=False, jetton_address=jetton_wallet.jetton)
        except Exception:
            return None

    async def build_block(self, block: Block, other_blocks: list[Block]) -> list[Block]:
        blocks = sorted(other_blocks, key=lambda x: x.min_lt)
        peer_swap_blocks = [block]
        in_transfer = None
        out_transfer = None
        for b in blocks:
            if isinstance(b, LabelBlock):
                match b.label:
                    case 'peer_swap':
                        peer_swap_blocks.append(b.block)
                    case 'payout':
                        peer_swap_blocks[-1] = (peer_swap_blocks[-1], b.block)
                    case 'out_transfer':
                        out_transfer = b.block
                    case 'in_transfer':
                        in_transfer = b.block
        ok = True
        actual_swap_steps = []
        destination_asset = None
        for (swap, pay_to) in peer_swap_blocks:
            pay_to_msg = StonfiV2PayTo(pay_to.get_body())
            if pay_to_msg.exit_code != 0xc64370e5:
                ok = False

            swap_msg = StonfiSwapV2(swap.get_body())
            actual_swap_steps.append((swap_msg.token_wallet1, swap.min_lt, pay_to_msg, swap_msg))
        actual_swap_steps.sort(key=lambda x: x[1])

        target_pool_account = actual_swap_steps[0][3].get_pool_accounts_recursive()[-1]
        target_pool_wallet = await context.interface_repository.get().get_jetton_wallet(target_pool_account)
        if target_pool_wallet is not None:
            if target_pool_wallet.jetton in PTonTransferMatcher.pton_masters:
                destination_asset = Asset(is_ton=True)
            else:
                destination_asset = Asset(is_ton=False, jetton_address=target_pool_wallet.jetton)

        swap_steps = []
        pool_addr_jetton_map = {}
        for wallet in actual_swap_steps:
            jetton = await context.interface_repository.get().get_jetton_wallet(wallet[0].to_str(False).upper())
            if jetton is not None:
                if jetton.jetton in PTonTransferMatcher.pton_masters:
                    asset = Asset(is_ton=True)
                else:
                    asset = Asset(is_ton=False, jetton_address=jetton.jetton)
                pool_addr_jetton_map[wallet[0].to_str(False).upper()] = asset
                swap_steps.append(asset)
            else:
                block.broken = True
                swap_steps = []
                break

        in_transfer_data = {}
        sender = None
        if isinstance(in_transfer, JettonTransferBlock):
            sender = in_transfer.data['sender']
            jetton_address = in_transfer.data['asset'].jetton_address
            if jetton_address.as_str() in PTonTransferMatcher.pton_masters:
                asset = Asset(is_ton=True)
            else:
                asset = Asset(is_ton=in_transfer.data['asset'].is_ton, jetton_address=jetton_address)
            in_transfer_data = {
                'asset': asset,
                'amount': in_transfer.data['amount'],
                'source': in_transfer.data['sender'],
                'source_jetton_wallet': in_transfer.data['sender_wallet'],
                'destination': in_transfer.data['receiver'],
                'destination_jetton_wallet': in_transfer.data['receiver_wallet']
            }
        else:
            message = in_transfer.event_nodes[0].message
            amount = message.value
            if message.opcode == PTonTransfer.opcode:
                amount = PTonTransfer(in_transfer.get_body()).ton_amount
            sender = AccountId(message.source)
            in_transfer_data = {
                'asset': Asset(is_ton=True, jetton_address=None),
                'amount': Amount(amount),
                'source': AccountId(message.source),
                'source_jetton_wallet': None,
                'destination': AccountId(block.event_nodes[0].message.source),
                'destination_jetton_wallet': AccountId(message.destination)
            }

        peer_swaps = []
        if len(actual_swap_steps) > 1:
            pay_to_msg = actual_swap_steps[0][2]
            assets = [(pay_to_msg.amount0_out, pay_to_msg.token0_address),
                      (pay_to_msg.amount1_out, pay_to_msg.token1_address)]
            assets.sort(key=lambda x: x[0], reverse=True)

            peer_swaps.append({
                'in': {
                    'amount': in_transfer_data['amount'],
                    'asset': in_transfer_data['asset']
                },
                'out': {
                    'amount': Amount(assets[0][0]),
                    'asset': pool_addr_jetton_map[assets[0][1].to_str(False).upper()]
                }
            })
            if pay_to_msg.exit_code == 0xc64370e5:
                for i in range(0, len(actual_swap_steps) - 1):
                    pay_to_msg = actual_swap_steps[i + 1][2]
                    assets = [(pay_to_msg.amount0_out, pay_to_msg.token0_address),
                              (pay_to_msg.amount1_out, pay_to_msg.token1_address)]
                    assets.sort(key=lambda x: x[0], reverse=True)
                    if pay_to_msg.exit_code != 0xc64370e5:
                        continue
                    peer_swaps.append({
                        'in': peer_swaps[-1]['out'],
                        'out': {
                            'amount': Amount(assets[0][0]),
                            'asset': pool_addr_jetton_map[assets[0][1].to_str(False).upper()]
                        }
                    })

        out_transfer_data = {}
        additional_blocks_to_include = []
        pton_transfer = next((x for x in out_transfer.next_blocks if isinstance(x, CallContractBlock)
                              and x.opcode == PTonTransfer.opcode), None)
        if pton_transfer is None and out_transfer.data['has_internal_transfer']:
            jetton_address = out_transfer.data['asset'].jetton_address
            if jetton_address.as_str() in PTonTransferMatcher.pton_masters:
                asset = Asset(is_ton=True)
            else:
                asset = Asset(is_ton=out_transfer.data['asset'].is_ton, jetton_address=jetton_address)
            out_transfer_data = {
                'asset': asset,
                'amount': out_transfer.data['amount'],
                'source': out_transfer.data['sender'],
                'source_jetton_wallet': out_transfer.data['sender_wallet'],
                'destination': out_transfer.data['receiver'],
                'destination_jetton_wallet': out_transfer.data['receiver_wallet']
            }
        else:
            additional_blocks_to_include.append(pton_transfer)
            amount = PTonTransfer(pton_transfer.get_body()).ton_amount
            out_transfer_data = {
                'asset': Asset(is_ton=True, jetton_address=None),
                'amount': Amount(amount),
                'source': out_transfer.data['sender'],
                'source_jetton_wallet': out_transfer.data['sender_wallet'],
                'destination': AccountId(pton_transfer.get_message().destination),
                'destination_jetton_wallet': None,
            }
        new_block = JettonSwapBlock({
            'dex': 'stonfi_v2',
            'source_asset': in_transfer_data['asset'],
            'destination_asset': out_transfer_data['asset'],
            'sender': sender,
            'dex_incoming_transfer': in_transfer_data,
            'dex_outgoing_transfer': out_transfer_data,
            'referral_amount': None,
            'referral_address': None,
            'peer_swaps': [] if len(peer_swaps) <= 1 else peer_swaps,
        })
        new_block.merge_blocks([block] + other_blocks + additional_blocks_to_include)
        new_block.failed = not ok
        if not ok:
            if destination_asset is not None:
                new_block.data['destination_asset'] = destination_asset
            else:
                target_asset = await self._get_target_asset_from_notification(
                    block.previous_block.event_nodes[0].message)
                if target_asset is not None:
                    new_block.data['destination_asset'] = target_asset
        return [new_block]


class DedustSwapBlockMatcher(BlockMatcher):
    def __init__(self):
        swap_matcher = OrMatcher([
            ContractMatcher(opcode=DedustSwapPeer.opcode,
                            children_matchers=[ContractMatcher(opcode=DedustSwapNotification.opcode,
                                                               optional=True)]),
            ContractMatcher(opcode=DedustSwapExternal.opcode,
                            children_matchers=[ContractMatcher(opcode=DedustSwapNotification.opcode,
                                                               optional=True)])
        ])
        peer_matcher = RecursiveMatcher(
            repeating_matcher=swap_matcher,
            exit_matcher=ContractMatcher(opcode=DedustPayoutFromPool.opcode,
                                         child_matcher=OrMatcher([
                                             ContractMatcher(opcode=DedustPayout.opcode),
                                             BlockTypeMatcher(block_type='jetton_transfer')
                                         ]))
        )
        payout = child_sequence_matcher([
            ContractMatcher(opcode=DedustPayoutFromPool.opcode),
            OrMatcher([BlockTypeMatcher(block_type='jetton_transfer'),
                       ContractMatcher(opcode=DedustPayout.opcode)])])
        super().__init__(optional=False,
                         children_matchers=[OrMatcher([payout, peer_matcher]),
                                            ContractMatcher(opcode=DedustSwapNotification.opcode,
                                                            optional=True)],
                         parent_matcher=OrMatcher([BlockTypeMatcher(block_type='jetton_transfer'),
                                                   ContractMatcher(opcode=DedustSwap.opcode)]))

    def test_self(self, block: Block):
        return isinstance(block, CallContractBlock) and block.opcode == DedustSwapExternal.opcode


    # Parse SwapStep
    def _parse_steps(self, slice: Slice):
        steps = []
        current_slice = slice.copy()
        while True:
            pool = AccountId(current_slice.load_address()).as_str()
            current_slice.load_bit()
            current_slice.load_coins()
            next_step = current_slice.load_maybe_ref()
            steps.append(pool)
            if next_step is not None:
                current_slice = next_step.to_slice()
            else:
                return steps

    # Find desired asset swap sequence
    async def _get_jetton_swap_sequence(self, steps, incoming_asset: Asset) -> list[Asset]:
        prev_step = incoming_asset
        seq = [prev_step]
        for step in steps:
            pool_info = await context.interface_repository.get().get_dedust_pool(step)
            for asset in pool_info.assets:
                if asset['is_ton'] and prev_step.is_ton:
                    continue
                if not prev_step.is_ton and asset['address'] == prev_step.jetton_address.as_str():
                    continue
                prev_step = Asset(asset['is_ton'], asset['address'] if not asset['is_ton'] else None)
                seq.append(prev_step)
                break
        return seq


    async def build_block(self, block: Block, other_blocks: list[Block]) -> list[Block]:
        new_block = JettonSwapBlock({})
        include = [block]
        for b in other_blocks:
            if isinstance(b, SingleLevelWrapper):
                include.extend(b.children_blocks)
            else:
                include.append(b)

        # Fill actual successful swaps
        messages = find_messages(include, DedustSwapNotification)
        messages.sort(key=lambda x: x[0].min_lt)
        peer_swaps = []
        for _, message in messages:
            data = {
                'in': {
                    'amount': Amount(message.amount_in),
                    'asset': Asset(is_ton=message.asset_in.is_ton,
                                   jetton_address=message.asset_in.jetton_address),
                },
                'out': {
                    'amount': Amount(message.amount_out),
                    'asset': Asset(is_ton=message.asset_out.is_ton,
                                   jetton_address=message.asset_out.jetton_address),
                }
            }
            peer_swaps.append(data)

        # Gather actual values from incoming transfer
        sender_jetton_transfer_block = None
        if (block.previous_block is not None and block.previous_block.btype == 'jetton_transfer'
                and block.previous_block in other_blocks):
            sender_jetton_transfer_block = block.previous_block
        sender = None
        sender_wallet = None
        dex_incoming_jetton_wallet = None
        dex_incoming_wallet = None
        amount_in = None
        asset_in = None
        swap_steps_slice = None
        if sender_jetton_transfer_block is not None:
            dex_incoming_jetton_wallet = sender_jetton_transfer_block.data['receiver_wallet']
            dex_incoming_wallet = sender_jetton_transfer_block.data['receiver']
            sender_wallet = sender_jetton_transfer_block.data['sender_wallet']
            sender = sender_jetton_transfer_block.data['sender']
            asset_in = sender_jetton_transfer_block.data['asset']
            if int(sender_jetton_transfer_block.data['payload_opcode'], 0) != DedustSwapPayload.opcode:
                return []
            amount_in = sender_jetton_transfer_block.data['amount']
            swap_steps_slice = Slice.one_from_boc(sender_jetton_transfer_block.data['forward_payload'])
            swap_steps_slice.skip_bits(32) # sum type
        else:
            swap_requests = find_call_contracts(other_blocks, DedustSwap.opcode)
            if len(swap_requests) > 0:
                sender = AccountId(swap_requests[0].get_message().source)
                dex_incoming_wallet = AccountId(swap_requests[0].get_message().destination)
                s = swap_requests[0].get_body()
                s.load_uint(32 + 64)
                amount_in = s.load_coins()
                swap_steps_slice = s
                asset_in = Asset(is_ton=True)

        # Gather actual values from payout
        payout_from_pool = find_call_contracts(other_blocks, DedustPayoutFromPool.opcode)
        assert len(payout_from_pool) == 1, "Expected one payout from pool"
        receiver = sender
        receiver_wallet = None
        dex_outgoing_jetton_wallet = None
        dex_outgoing_wallet = None
        actual_amount_out = DedustPayoutFromPool(payout_from_pool[0].get_body()).amount
        actual_asset_out = None
        for payout_block in payout_from_pool[0].next_blocks:
            if payout_block in include:
                if isinstance(payout_block, JettonTransferBlock):
                    payout = payout_block
                    receiver_wallet = payout.data['receiver_wallet']
                    receiver = payout.data['receiver']
                    dex_outgoing_wallet = payout.data['sender']
                    dex_outgoing_jetton_wallet = payout.data['sender_wallet']
                    actual_asset_out = payout.data['asset']
                    actual_amount_out = payout.data['amount']
                elif isinstance(payout_block, CallContractBlock) and payout_block.opcode == DedustPayout.opcode:
                    payout = payout_block
                    dex_outgoing_wallet = AccountId(payout.get_message().source)
                    receiver = AccountId(payout.get_message().destination)
                    actual_asset_out = Asset(is_ton=True)

        # Find desired out asset
        steps = self._parse_steps(swap_steps_slice)
        swap_sequence = await self._get_jetton_swap_sequence(steps, Asset(is_ton=True) if asset_in is None else asset_in)
        asset_out = swap_sequence[-1]

        # Fill peer swaps with actual values if there is no any successful swap
        if len(peer_swaps) == 0:
            peer_swaps.append({
                'in': {
                    'amount': Amount(amount_in),
                    'asset': asset_in
                },
                'out': {
                    'amount': Amount(actual_amount_out),
                    'asset': actual_asset_out
                }
            })

        # Check that every "swap request" has a corresponding swap notification, else mark as failed
        failed = False
        for b in [block] + other_blocks:
            if isinstance(b, CallContractBlock) and b.opcode in (DedustSwapExternal.opcode, DedustSwapPeer.opcode):
                has_swap_notification = False
                for n in b.next_blocks:
                    if n in include and isinstance(n, CallContractBlock) and n.opcode == DedustSwapNotification.opcode:
                        has_swap_notification = True
                        break
                if not has_swap_notification:
                    failed = True
                    break

        data = {
            'dex': 'dedust',
            'sender': sender,
            'dex_incoming_transfer': {
                'asset': peer_swaps[0]['in']['asset'],
                'amount': peer_swaps[0]['in']['amount'],
                'source': sender,
                'source_jetton_wallet': sender_wallet,
                'destination': dex_incoming_wallet,
                'destination_jetton_wallet': dex_incoming_jetton_wallet,
            },
            'dex_outgoing_transfer': {
                'asset': peer_swaps[-1]['out']['asset'],
                'amount': peer_swaps[-1]['out']['amount'],
                'source': dex_outgoing_wallet,
                'source_jetton_wallet': dex_outgoing_jetton_wallet,
                'destination': receiver,
                'destination_jetton_wallet': receiver_wallet,
            },
            'in': peer_swaps[0]['in'],
            'out': peer_swaps[-1]['out'],
            'peer_swaps': peer_swaps if len(peer_swaps) > 1 else [],
            'source_asset': asset_in,
            'destination_asset': asset_out,
        }
        new_block.merge_blocks(include)
        new_block.data = data
        new_block.failed = failed
        return [new_block]


class ToncoSwapBlockMatcher(BlockMatcher):
    swap_opcode = ToncoPoolV3Swap.opcode
    pay_to_opcode = ToncoRouterV3PayTo.opcode
    pton_incoming_transfer_opcode = 0x01F3835D

    def __init__(self):
        # almost the same as StonfiV2SwapBlockMatcher, but
        # - using Tonco opcodes
        # - multihops are like literally one swap after another,
        #   so router sends tokens to itself to continue

        # 1. incoming transfer
        in_pton_transfer = ContractMatcher(
            opcode=JettonNotify.opcode,
            parent_matcher=labeled(
                "in_transfer",
                ContractMatcher(opcode=self.pton_incoming_transfer_opcode),
            ),
        )
        in_transfer = OrMatcher(
            [
                labeled("in_transfer", BlockTypeMatcher(block_type="jetton_transfer")),
                in_pton_transfer,
            ]
        )

        # 2. peer swap (recursive part) - the basis for the next steps
        # peer_swap_matcher (POOLV3_SWAP) itself is the entry point for test_self
        # or a child element for intermediate_notify_then_swap.
        _peer_swap_core_matcher = ContractMatcher(
            self.swap_opcode, child_matcher=None
        )  # not optional here, because it's part of the required chain
        peer_swap_matcher = labeled("peer_swap", _peer_swap_core_matcher)

        # 3. the key element for multi-hop via self-notification:
        # JettonNotify, followed by a STRICTLY next POOLV3_SWAP.
        # This block is NOT optional, because if we go down this path, it must be present.
        intermediate_notify_then_swap = labeled(
            "intermediate_hop_trigger",
            ContractMatcher(
                opcode=JettonNotify.opcode,
                child_matcher=peer_swap_matcher,  # strictly followed by the next POOLV3_SWAP
                optional=False,
            ),  # NOT optional
        )

        # 4. matchers for outgoing transfers that DEFINITELY lead to the next hop
        # they can be optional in the overall list, but their child elements (intermediate_notify_then_swap) are not.
        # Though it may seem a curious tale to some,
        # yet in this matcher, after the jetton_transfer block,
        # follows the opcode PTonTransfer, and then JettonNotify.
        # Tis a feature of the PTonTransferMatcher, whereupon,
        # when the router sends pton unto itself,
        # it doth mark the action as jetton_transfer solely for
        # the JettonTransfer message and naught beyond.
        out_pton_DEFINITELY_leading_to_hop = BlockTypeMatcher(
            block_type="jetton_transfer",
            optional=False,
            child_matcher=ContractMatcher(
                opcode=PTonTransfer.opcode,
                optional=False,
                child_matcher=intermediate_notify_then_swap,
            ),
        )
        out_jetton_DEFINITELY_leading_to_hop = BlockTypeMatcher(
            block_type="jetton_transfer",
            optional=False,
            child_matcher=peer_swap_matcher,
        )

        # 5. matchers for FINAL outgoing transfers
        # important: they should NOT have a child_matcher that could catch the next hop.
        final_out_pton_matcher = ContractMatcher(
            opcode=JettonTransfer.opcode,
            optional=True,
            child_matcher=ContractMatcher(
                opcode=PTonTransfer.opcode, child_matcher=None
            ),
        )
        final_out_jetton_matcher = BlockTypeMatcher(
            block_type="jetton_transfer", child_matcher=None, optional=True
        )

        # 6. what can follow the payout (ROUTERV3_PAY_TO).
        # the order in OrMatcher is important!
        out_options_after_payout = OrMatcher(
            [
                # first try to catch transfers leading to the next hop
                labeled("intermediate_transfer", out_pton_DEFINITELY_leading_to_hop),
                labeled("intermediate_transfer", out_jetton_DEFINITELY_leading_to_hop),
                # if it's not a hop, then it must be a final transfer
                labeled("out_transfer", final_out_pton_matcher),
                labeled("out_transfer", final_out_jetton_matcher),
            ]
        )

        # 7. the payout message from the pool (ROUTERV3_PAY_TO)
        payout_matcher = labeled(
            "payout",
            ContractMatcher(
                self.pay_to_opcode,
                child_matcher=out_options_after_payout,
                optional=False,
            ),
        )  # Payout is required

        # 8. connect _peer_swap_core_matcher with payout_matcher for recursion
        peer_swap_matcher.child_matcher = payout_matcher

        # 9. initialize the base matcher
        super().__init__(
            parent_matcher=in_transfer, optional=False, child_matcher=payout_matcher
        )

    def test_self(self, block: Block):
        return isinstance(block, CallContractBlock) and block.opcode == self.swap_opcode

    async def build_block(self, block: Block, other_blocks: list[Block]) -> list[Block]:
        """Builds a JettonSwapBlock from the matched blocks in a Tonco swap operation.

        Handles multi-hop swaps where router transfers tokens to itself to continue the swap chain.
        The unique aspects of Tonco protocol:
        1. Router may transfer pTON to itself to continue swap chain
        2. PTonTransfer followed by JettonNotify indicates an intermediate hop
        3. Final transfers can be either regular Jetton or JettonTransfer + PTonTransfer
        """
        blocks = sorted(other_blocks, key=lambda x: x.min_lt)

        in_transfer: Block | None = None
        payouts: list[Block] = []
        peer_swaps: list[Block] = []
        out_transfers: list[Block] = []
        intermediate_transfers: list[Block] = []
        intermediate_hop_triggers: list[Block] = []

        # collect all labeled blocks
        for b in blocks:
            if isinstance(b, LabelBlock):
                bb = b.block
                match b.label:
                    case "in_transfer":
                        in_transfer = bb
                    case "peer_swap":
                        peer_swaps.append(bb)
                    case "payout":
                        payouts.append(bb)
                    case "out_transfer":
                        out_transfers.append(bb)
                    case "intermediate_transfer":
                        intermediate_transfers.append(bb)
                    case "intermediate_hop_trigger":
                        intermediate_hop_triggers.append(bb)

        # add the initial block (first swap)
        peer_swaps.insert(0, block)

        all_swaps = sorted(peer_swaps, key=lambda x: x.min_lt)
        all_payouts = sorted(payouts, key=lambda x: x.min_lt)

        # basic validation
        if not in_transfer or len(all_swaps) != len(all_payouts):
            logger.warning(
                f"Incomplete Tonco swap chain: in_transfer={in_transfer is not None}, "
                f"swaps={len(all_swaps)}, payouts={len(all_payouts)}"
            )
            return []

        in_transfer_block = in_transfer

        # match swaps with their payouts based on order
        swap_payout_pairs = list(zip(all_swaps, all_payouts))

        ok = True
        swap_steps = []
        jetton_wallet_asset_map: dict[str, Asset] = {}  # jwallet address -> Asset

        # process each swap+payout pair to extract swap step details
        for i, (swap_block, payout_block) in enumerate(swap_payout_pairs):
            try:
                swap_msg = ToncoPoolV3Swap(swap_block.get_body())
                payout_msg = ToncoRouterV3PayTo(payout_block.get_body())

                # check success
                if payout_msg.exit_code != 0 and payout_msg.exit_code != 200:
                    ok = False

                source_wallet = AccountId(swap_msg.source_wallet)

                swap_steps.append(
                    {
                        "swap_block": swap_block,
                        "payout_block": payout_block,
                        "source_wallet": source_wallet,
                        "swap_msg": swap_msg,
                        "payout_msg": payout_msg,
                        "min_lt": swap_block.min_lt,
                    }
                )

                # try to determine asset type for this wallet
                if source_wallet.as_str() not in jetton_wallet_asset_map:
                    jetton_wallet = (
                        await context.interface_repository.get().get_jetton_wallet(
                            source_wallet.as_str() or ""
                        )
                    )
                    if jetton_wallet is not None:
                        if jetton_wallet.jetton in PTonTransferMatcher.pton_masters:
                            asset = Asset(is_ton=True)
                        else:
                            asset = Asset(
                                is_ton=False, jetton_address=jetton_wallet.jetton
                            )
                        jetton_wallet_asset_map[source_wallet.as_str() or ""] = asset

            except Exception as e:
                logger.warning(
                    f"Error processing Tonco swap step {i}: {e}", exc_info=True
                )
                ok = False

        swap_steps.sort(key=lambda x: x["min_lt"])

        # process incoming transfer data
        in_transfer_data = {}
        sender = None
        in_transfer_payload = begin_cell().end_cell()

        if isinstance(in_transfer_block, JettonTransferBlock):
            sender = in_transfer_block.data.get("sender")
            jetton_address = in_transfer_block.data.get("asset").jetton_address

            # check if it's pTON
            is_pton = False
            if (
                jetton_address
                and jetton_address.as_str() in PTonTransferMatcher.pton_masters
            ):
                is_pton = True

            asset = Asset(
                is_ton=is_pton, jetton_address=None if is_pton else jetton_address
            )

            in_transfer_data = {
                "asset": asset,
                "amount": in_transfer_block.data.get("amount"),
                "source": in_transfer_block.data.get("sender"),
                "source_jetton_wallet": in_transfer_block.data.get("sender_wallet"),
                "destination": in_transfer_block.data.get("receiver"),
                "destination_jetton_wallet": in_transfer_block.data.get(
                    "receiver_wallet"
                ),
            }
            payload = in_transfer_block.data.get("forward_payload")
            if isinstance(payload, Cell):
                in_transfer_payload = payload
        else:
            # handle incoming TON or pTON transfer via notification
            try:
                message = in_transfer_block.event_nodes[0].message
                amount = message.value
                forward_payload = None

                if message.opcode == PTonTransfer.opcode:
                    pton_message = PTonTransfer(in_transfer_block.get_body())
                    amount = pton_message.ton_amount
                    forward_payload = pton_message.forward_payload

                sender = AccountId(message.source)
                in_transfer_data = {
                    "asset": Asset(is_ton=True, jetton_address=None),
                    "amount": Amount(amount or 0),
                    "source": AccountId(message.source),
                    "source_jetton_wallet": None,
                    "destination": AccountId(block.event_nodes[0].message.source),
                    "destination_jetton_wallet": AccountId(message.destination),
                }
                if forward_payload:
                    in_transfer_payload = forward_payload
            except Exception as e:
                logger.warning(
                    f"Error processing Tonco swap incoming (ton) transfer: {e}",
                    exc_info=True,
                )
                return []

        # process outgoing transfer (the final one to user)
        out_transfer = None
        if out_transfers:
            out_transfer = out_transfers[-1]

        if not out_transfer:
            logger.warning("No outgoing transfer found in Tonco swap")
            return []

        # process outgoing transfer
        out_transfer_data = {}
        additional_blocks = []

        # incude pton transfer block if it's there
        pton_transfer = find_call_contract(
            out_transfer.next_blocks, PTonTransfer.opcode
        )
        if pton_transfer:
            additional_blocks.append(pton_transfer)

        if isinstance(out_transfer, JettonTransferBlock):
            try:
                jetton_address = out_transfer.data.get("asset").jetton_address
                is_pton = False
                if (
                    jetton_address
                    and jetton_address.as_str() in PTonTransferMatcher.pton_masters
                ):
                    is_pton = True

                asset = Asset(
                    is_ton=is_pton, jetton_address=None if is_pton else jetton_address
                )

                # got it - so save just in case
                receiver_wallet = out_transfer.data.get("receiver_wallet")
                sender_wallet = out_transfer.data.get("sender_wallet")
                if receiver_wallet:
                    jetton_wallet_asset_map[receiver_wallet.as_str()] = asset
                if sender_wallet:
                    jetton_wallet_asset_map[sender_wallet.as_str()] = asset

                out_transfer_data = {
                    "asset": asset,
                    "amount": out_transfer.data.get("amount"),
                    "source": out_transfer.data.get("sender"),
                    "source_jetton_wallet": sender_wallet,
                    "destination": out_transfer.data.get("receiver"),
                    "destination_jetton_wallet": receiver_wallet,
                }
            except Exception as e:
                logger.warning(
                    f"Error processing Tonco jetton outgoing transfer: {e}",
                    exc_info=True,
                )
                return []
        else:
            logger.warning(
                f"Unsupported outgoing transfer type in Tonco swap: {type(out_transfer)}"
            )
            return []

        # process intermediate_hop_triggers - these are the JettonNotify messages
        # that indicate router is continuing to another swap
        intermediate_data = []
        for trigger in intermediate_hop_triggers:
            if (
                isinstance(trigger, CallContractBlock)
                and trigger.opcode == JettonNotify.opcode
            ):
                intermediate_data.append({"block": trigger, "min_lt": trigger.min_lt})

        # process intermediate_transfers - these contain PTonTransfer when router sends pTON to itself
        for transfer in intermediate_transfers:
            if isinstance(transfer, JettonTransferBlock):
                intermediate_data.append(
                    {
                        "block": transfer,
                        "min_lt": transfer.min_lt,
                        "next_blocks": transfer.next_blocks,
                    }
                )

                # may be pTON too
                pton_transfer = find_call_contract(
                    transfer.next_blocks, PTonTransfer.opcode
                )
                if pton_transfer:
                    additional_blocks.append(pton_transfer)

        intermediate_data.sort(key=lambda x: x["min_lt"])

        # build peer_swaps information for multi-hop
        peer_swap_data = []
        if len(swap_steps) > 1:
            # first step: incoming transfer to first pool
            first_step = swap_steps[0]
            first_payout = first_step["payout_msg"]

            assets = []
            if (
                first_payout.amount0 is not None
                and first_payout.jetton0_address is not None
            ):
                assets.append(
                    (first_payout.amount0, AccountId(first_payout.jetton0_address))
                )
            if (
                first_payout.amount1 is not None
                and first_payout.jetton1_address is not None
            ):
                assets.append(
                    (first_payout.amount1, AccountId(first_payout.jetton1_address))
                )

            assets.sort(key=lambda x: x[0] or 0, reverse=True)
            if assets:
                out_amount, out_addr = assets[0]
                out_asset = jetton_wallet_asset_map.get(out_addr.as_str())

                if out_asset:
                    # add first step
                    first_hop = {
                        "in": {
                            "amount": in_transfer_data["amount"],
                            "asset": in_transfer_data["asset"],
                        },
                        "out": {"amount": Amount(out_amount or 0), "asset": out_asset},
                    }
                    peer_swap_data.append(first_hop)

                    # process subsequent hops
                    prev_hop_out = first_hop["out"]

                    # process subsequent hops
                    for next_step in swap_steps[1:]:
                        next_payout = next_step["payout_msg"]

                        if next_payout.exit_code != 0 and next_payout.exit_code != 200:
                            # already marked ok=False above
                            continue

                        assets = []
                        if (
                            next_payout.amount0 is not None
                            and next_payout.jetton0_address is not None
                        ):
                            assets.append(
                                (
                                    next_payout.amount0,
                                    AccountId(next_payout.jetton0_address),
                                )
                            )
                        if (
                            next_payout.amount1 is not None
                            and next_payout.jetton1_address is not None
                        ):
                            assets.append(
                                (
                                    next_payout.amount1,
                                    AccountId(next_payout.jetton1_address),
                                )
                            )

                        assets.sort(key=lambda x: x[0] or 0, reverse=True)
                        if assets:
                            next_out_amount, next_out_addr = assets[0]
                            next_out_asset = jetton_wallet_asset_map.get(
                                next_out_addr.as_str()
                            )
                            next_hop = {
                                "in": prev_hop_out.copy(),  # use previous hop output as input
                                "out": {
                                    "amount": Amount(next_out_amount or 0),
                                    "asset": next_out_asset or Asset(is_ton=True),
                                },
                            }
                            peer_swap_data.append(next_hop)
                            prev_hop_out = next_hop["out"]  # update for next hop

        destination_asset = out_transfer_data["asset"]
        min_out_amount = out_transfer_data["amount"]
        # if the swap failed, extract target_jetton_wallet from original swap message
        if not ok and swap_steps:
            try:
                swap_payload = ToncoPoolV3SwapPayload(in_transfer_payload.begin_parse())
                target_wallets = swap_payload.get_target_wallets_and_amounts_recursive()
                target_jetton_wallet, min_out_amount = (
                    target_wallets[-1] if target_wallets else (None, None)
                )
                if target_jetton_wallet:
                    target_asset = jetton_wallet_asset_map.get(target_jetton_wallet)
                    if not target_asset:
                        try:
                            target_wallet = await context.interface_repository.get().get_jetton_wallet(
                                target_jetton_wallet
                            )
                            if target_wallet is not None:
                                if (
                                    target_wallet.jetton
                                    in PTonTransferMatcher.pton_masters
                                ):
                                    target_asset = Asset(is_ton=True)
                                else:
                                    target_asset = Asset(
                                        is_ton=False,
                                        jetton_address=target_wallet.jetton,
                                    )
                        except Exception as e:
                            logger.warning(
                                f"Error determining target asset for failed swap: {e}"
                            )

                    if target_asset:
                        destination_asset = target_asset
                        min_out_amount = Amount(min_out_amount or 0)
            except Exception as e:
                logger.info(f"Error determining target asset for failed swap: {e}")

        # final block
        new_block = JettonSwapBlock(
            {
                "dex": "tonco",
                "source_asset": in_transfer_data["asset"],
                "destination_asset": destination_asset,
                "sender": sender,
                "dex_incoming_transfer": in_transfer_data,
                "dex_outgoing_transfer": out_transfer_data,
                "referral_amount": None,
                "referral_address": None,
                "peer_swaps": peer_swap_data,
                "min_out_amount": min_out_amount,
            }
        )

        blocks_to_include = [block] + other_blocks + additional_blocks

        all_labeled_blocks = (
            peer_swaps
            + payouts
            + out_transfers
            + intermediate_transfers
            + intermediate_hop_triggers
        )
        if in_transfer:
            all_labeled_blocks.append(in_transfer)

        for b in all_labeled_blocks:
            if b not in blocks_to_include:
                blocks_to_include.append(b)

        new_block.merge_blocks(blocks_to_include)
        new_block.failed = not ok

        return [new_block]


class CoffeeSwapBlockMatcher(BlockMatcher):
    def __init__(self):
        # 1. incoming transfer: either a jetton transfer or a native swap call
        in_transfer_matcher = labeled(
            "in_transfer",
            OrMatcher(
                [
                    BlockTypeMatcher(block_type="jetton_transfer"),
                    ContractMatcher(opcode=CoffeeSwapNative.opcode),
                ]
            ),
        )

        # 2. outgoing transfer (final step)
        out_transfer_matcher = labeled(
            "out_transfer",
            OrMatcher(
                [
                    BlockTypeMatcher(block_type="jetton_transfer"),
                    ContractMatcher(opcode=CoffeePayout.opcode),
                    ContractMatcher(opcode=CoffeeNotification.opcode),
                ]
            ),
        )

        # 3. payout from pool (precedes final transfer)
        payout_matcher = labeled(
            "payout",
            ContractMatcher(
                opcode=CoffeePayoutInternal.opcode, child_matcher=out_transfer_matcher
            ),
        )

        # 4. repeating part of the chain: a swap followed by its success event
        repeating_swap_matcher = ContractMatcher(
            opcode=CoffeeSwapInternal.opcode,
            child_matcher=ContractMatcher(opcode=CoffeeSwapSuccessfulEvent.opcode),
        )

        # 5. the recursive part that finds all intermediate swaps
        peer_swap_chain_matcher = RecursiveMatcher(
            repeating_matcher=repeating_swap_matcher,
            exit_matcher=payout_matcher,
            optional=True,  # zero intermediate hops is a valid case
        )

        # 6. the anchor swap must also have a success event,
        # and it's followed by the rest of the chain
        anchor_swap_children_matcher = OrMatcher(
            [
                peer_swap_chain_matcher,  # case with intermediate hops
                payout_matcher,  # case with no intermediate hops
            ]
        )

        super().__init__(
            parent_matcher=in_transfer_matcher,
            optional=False,
            children_matchers=[
                ContractMatcher(opcode=CoffeeSwapSuccessfulEvent.opcode),
                anchor_swap_children_matcher,
            ],
        )

    def test_self(self, block: Block):
        # the anchor is the first CoffeeSwapInternal in the chain
        return (
            isinstance(block, CallContractBlock)
            and block.opcode == CoffeeSwapInternal.opcode
        )

    async def build_block(
        self, block: Block, other_blocks: list[Block]
    ) -> list[Block]:
        # 1. collect all labeled blocks and other relevant blocks
        in_transfer_block = get_labeled("in_transfer", other_blocks)
        payout_block = get_labeled("payout", other_blocks)
        out_transfer_block = get_labeled("out_transfer", other_blocks)

        if not all((in_transfer_block, payout_block, out_transfer_block)):
            return []

        # 2. find all swap steps and their corresponding success events
        swap_internal_blocks = find_call_contracts(
            other_blocks, CoffeeSwapInternal.opcode
        )
        if isinstance(block, CallContractBlock):
            swap_internal_blocks.append(block)

        ok = True
        peer_swaps = []

        # sort swaps by lt to process them in order
        sorted_swap_blocks = sorted(set(swap_internal_blocks), key=lambda b: b.min_lt)

        for swap_block in sorted_swap_blocks:
            # each swap_internal MUST be followed by a success_event
            event_block = find_call_contract(swap_block.next_blocks, CoffeeSwapSuccessfulEvent.opcode)

            if not event_block:
                ok = False
                continue  # if one hop fails, the whole swap is considered failed

            event_msg = CoffeeSwapSuccessfulEvent(event_block.get_body())

            peer_swaps.append(
                {
                    "in": {
                        "asset": event_msg.input,
                        "amount": Amount(event_msg.input_amount or 0),
                    },
                    "out": {
                        "asset": None,  # we will determine this from the next hop or final payout
                        "amount": Amount(event_msg.output_amount or 0),
                    },
                }
            )

        if not peer_swaps:
            return []

        # 3. parse incoming transfer
        in_transfer_data = {}
        sender = None
        if isinstance(in_transfer_block, JettonTransferBlock):
            sender = in_transfer_block.data["sender"]
            in_transfer_data = {
                "asset": in_transfer_block.data["asset"],
                "amount": in_transfer_block.data["amount"],
                "source": in_transfer_block.data["sender"],
                "source_jetton_wallet": in_transfer_block.data["sender_wallet"],
                "destination": in_transfer_block.data["receiver"],
                "destination_jetton_wallet": in_transfer_block.data[
                    "receiver_wallet"
                ],
            }
        elif (
            isinstance(in_transfer_block, CallContractBlock)
            and in_transfer_block.opcode == CoffeeSwapNative.opcode
        ):
            msg = CoffeeSwapNative(in_transfer_block.get_body())
            sender = AccountId(in_transfer_block.get_message().source)
            in_transfer_data = {
                "asset": Asset(is_ton=True),
                "amount": Amount(msg.amount or 0),
                "source": sender,
                "source_jetton_wallet": None,
                "destination": AccountId(in_transfer_block.get_message().destination),
                "destination_jetton_wallet": None,
            }

        # 4. parse outgoing transfer
        out_transfer_data = {}
        if isinstance(out_transfer_block, JettonTransferBlock):
            out_transfer_data = {
                "asset": out_transfer_block.data["asset"],
                "amount": out_transfer_block.data["amount"],
                "source": out_transfer_block.data["sender"],
                "source_jetton_wallet": out_transfer_block.data["sender_wallet"],
                "destination": out_transfer_block.data["receiver"],
                "destination_jetton_wallet": out_transfer_block.data.get(
                    "receiver_wallet"
                ),
            }
        elif (
            isinstance(out_transfer_block, CallContractBlock)
            and out_transfer_block.opcode == CoffeePayout.opcode
            and payout_block
        ):
            payout_internal_msg = CoffeePayoutInternal(payout_block.get_body())
            out_transfer_data = {
                "asset": Asset(is_ton=True),
                "amount": Amount(payout_internal_msg.amount or 0),
                "source": AccountId(payout_block.get_message().source),
                "source_jetton_wallet": None,
                "destination": AccountId(payout_internal_msg.recipient),
                "destination_jetton_wallet": None,
            }
        elif (
            isinstance(out_transfer_block, CallContractBlock)
            and out_transfer_block.opcode == CoffeeNotification.opcode
        ):
            message = out_transfer_block.get_message()
            out_transfer_data = {
                "asset": Asset(is_ton=True),
                "amount": Amount(message.value or 0),
                "source": AccountId(message.source),
                "source_jetton_wallet": None,
                "destination": AccountId(message.destination),
                "destination_jetton_wallet": None,
            }

        # fill in the missing 'out' asset info for peer_swaps
        for i in range(len(peer_swaps) - 1):
            peer_swaps[i]["out"]["asset"] = peer_swaps[i + 1]["in"]["asset"]

        if out_transfer_data:
            peer_swaps[-1]["out"]["asset"] = out_transfer_data.get("asset")

        # 5. create the final swap block
        final_data = {
            "dex": "coffee",
            "sender": sender,
            "source_asset": in_transfer_data.get("asset"),
            "destination_asset": out_transfer_data.get("asset"),
            "dex_incoming_transfer": in_transfer_data,
            "dex_outgoing_transfer": out_transfer_data,
            "peer_swaps": peer_swaps if len(peer_swaps) > 1 else [],
            "referral_amount": None,
            "referral_address": None,
        }

        new_block = JettonSwapBlock(final_data)
        new_block.merge_blocks([block] + other_blocks)
        new_block.failed = not ok

        return [new_block]
