// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import "../src/Escrow.sol";

contract CheckpointsTest is Test {
    Escrow escrow;
    address provider = address(11);
    address party = address(12);
    bytes32 sid = keccak256("session");

    function setUp() public {
        escrow = new Escrow(address(13));
        vm.prank(provider);
        escrow.openSession(sid, party);
    }

    function test_checkpoints_do_not_start_final_clock() public {
        vm.prank(provider);
        escrow.checkpointTrace(sid, 10, keccak256("prefix"));
        (, , bytes32 finalHash, , uint64 committedAt, ) = escrow.sessions(sid);
        assertEq(finalHash, bytes32(0));
        assertEq(committedAt, 0);
        assertEq(escrow.checkpointCount(sid), 1);
        (uint256 count, bytes32 h) = escrow.traceCheckpoints(sid, 0);
        assertEq(count, 10);
        assertEq(h, keccak256("prefix"));
        vm.warp(block.timestamp + 60);
        vm.prank(provider);
        escrow.commitTrace(sid, keccak256("final"));
        (, , , , committedAt, ) = escrow.sessions(sid);
        assertEq(committedAt, block.timestamp);
    }

    function test_only_provider_and_increasing_nonzero_prefixes() public {
        vm.expectRevert(bytes("not session provider"));
        escrow.checkpointTrace(sid, 1, keccak256("x"));
        vm.startPrank(provider);
        vm.expectRevert(bytes("empty checkpoint"));
        escrow.checkpointTrace(sid, 0, keccak256("x"));
        vm.expectRevert(bytes("zero checkpoint hash"));
        escrow.checkpointTrace(sid, 1, bytes32(0));
        escrow.checkpointTrace(sid, 2, keccak256("x"));
        vm.expectRevert(bytes("checkpoint count not increasing"));
        escrow.checkpointTrace(sid, 1, keccak256("y"));
        vm.expectRevert(bytes("checkpoint count not increasing"));
        escrow.checkpointTrace(sid, 2, keccak256("z"));
        escrow.commitTrace(sid, keccak256("final"));
        vm.expectRevert(bytes("trace already committed"));
        escrow.checkpointTrace(sid, 3, keccak256("late"));
        vm.stopPrank();
    }
}
