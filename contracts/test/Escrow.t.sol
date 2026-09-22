// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Escrow} from "../src/Escrow.sol";

contract EscrowTest is Test {
    Escrow escrow;
    address owner = makeAddr("owner");
    address verifier = makeAddr("verifier");
    address provider = makeAddr("provider");
    address challenger = makeAddr("challenger");

    bytes32 constant SID = keccak256("session-1");
    bytes32 constant SID2 = keccak256("session-2");
    bytes32 constant SID3 = keccak256("session-3");
    bytes32 constant TRACE = keccak256("trace");
    bytes32 constant PRED = keccak256("predicate");
    bytes32 constant PARAMS = keccak256("params");
    uint256 constant PAYOUT = 0.1 ether;
    uint256 constant CBOND = 0.01 ether;

    function setUp() public {
        vm.prank(owner);
        escrow = new Escrow(verifier);
        vm.deal(provider, 10 ether);
        vm.deal(challenger, 1 ether);
    }

    function _register(uint256 reserve) internal returns (uint256 pid) {
        vm.prank(provider);
        pid = escrow.registerPromise{value: reserve}(PRED, PARAMS, PAYOUT);
    }

    function _openCommit(bytes32 sid) internal {
        vm.startPrank(provider);
        escrow.openSession(sid, challenger);
        escrow.commitTrace(sid, keccak256(abi.encode(sid, "trace")));
        vm.stopPrank();
    }

    function _promiseSession() internal returns (uint256 pid) {
        pid = _register(10 * PAYOUT);
        _openCommit(SID);
    }

    // ── settlement paths ──────────────────────────────────────────────────────

    function test_valid_challenge_drains_reserve_and_pays_challenger() public {
        uint256 pid = _promiseSession();
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);

        uint256 challengerBefore = challenger.balance;
        (, , , , uint256 reserveBefore, , ) = escrow.promises(pid);

        vm.prank(verifier);
        escrow.submitVerdict(cid, true);

        (, , , , uint256 reserveAfter, , ) = escrow.promises(pid);
        assertEq(reserveAfter, reserveBefore - PAYOUT, "reserve drained by one payout");
        assertEq(challenger.balance, challengerBefore + PAYOUT + CBOND, "challenger gets payout + bond back");
        assertEq(escrow.openChallenges(pid), 0, "counter closed");
    }

    function test_invalid_challenge_credits_bond_to_provider() public {
        uint256 pid = _promiseSession();
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);

        vm.prank(verifier);
        escrow.submitVerdict(cid, false);

        assertEq(escrow.bonds(provider), CBOND, "forfeited bond accrues as credit");
        uint256 before = provider.balance;
        vm.prank(provider);
        escrow.withdrawBond(CBOND);
        assertEq(provider.balance, before + CBOND, "credit withdrawable");
    }

    function test_only_verifier_can_submit_verdict() public {
        uint256 pid = _promiseSession();
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);

        vm.prank(challenger);
        vm.expectRevert(bytes("not verifier"));
        escrow.submitVerdict(cid, true);
    }

    function test_only_session_party_can_challenge() public {
        uint256 pid = _promiseSession();
        address stranger = makeAddr("stranger");
        vm.deal(stranger, 1 ether);

        vm.prank(stranger);
        vm.expectRevert(bytes("not session party"));
        escrow.challenge{value: CBOND}(SID, pid);
    }

    function test_double_verdict_reverts() public {
        uint256 pid = _promiseSession();
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);

        vm.prank(verifier);
        escrow.submitVerdict(cid, true);
        vm.prank(verifier);
        vm.expectRevert(bytes("not open"));
        escrow.submitVerdict(cid, true);
    }

    function test_register_born_funded() public {
        vm.prank(provider);
        vm.expectRevert(bytes("initial reserve below payout"));
        escrow.registerPromise{value: PAYOUT - 1}(PRED, PARAMS, PAYOUT);
    }

    function test_cross_provider_challenge_reverts() public {
        uint256 pid = _promiseSession();
        pid; // provider's session SID
        address attacker = makeAddr("attacker");
        vm.deal(attacker, 1 ether);
        vm.prank(attacker);
        uint256 evilPid = escrow.registerPromise{value: PAYOUT}(PRED, PARAMS, PAYOUT);

        vm.prank(challenger);
        vm.expectRevert(bytes("promise/session provider mismatch"));
        escrow.challenge{value: CBOND}(SID, evilPid);
    }

    function test_replay_same_pair_reverts() public {
        uint256 pid = _promiseSession();          // reserve 10x: promise still in force after slash
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);
        vm.prank(verifier);
        escrow.submitVerdict(cid, true);

        vm.prank(challenger);
        vm.expectRevert(bytes("pair already resolved"));
        escrow.challenge{value: CBOND}(SID, pid);
    }

    function test_satisfied_final_trace_cannot_be_rechallenged_for_default_payout() public {
        uint256 pid = _promiseSession();
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);
        vm.prank(verifier);
        escrow.submitVerdict(cid, false);

        // A later outage must not turn the same cleared trace into a default payout.
        vm.warp(block.timestamp + escrow.RESPONSE_WINDOW() + 1);
        vm.prank(challenger);
        vm.expectRevert(bytes("pair already resolved"));
        escrow.challenge{value: CBOND}(SID, pid);

        (, , , , uint256 reserve, , ) = escrow.promises(pid);
        assertEq(reserve, 10 * PAYOUT, "cleared trace cannot drain reserve");
        assertEq(escrow.bonds(provider), CBOND, "only the adjudicated bond is forfeited");
        assertEq(escrow.openChallenges(pid), 0, "no new challenge pins the reserve");
    }

    function test_nonfinal_satisfaction_does_not_clear_the_eventual_trace() public {
        uint256 pid = _register(10 * PAYOUT);
        vm.startPrank(provider);
        escrow.openSession(SID, challenger);
        escrow.checkpointTrace(SID, 1, keccak256("incomplete prefix"));
        vm.stopPrank();
        vm.prank(challenger);
        uint256 first = escrow.challenge{value: CBOND}(SID, pid);
        vm.prank(verifier);
        escrow.submitVerdict(first, false);

        // No verdict about an unfinished session may clear actions recorded later.
        vm.prank(provider);
        escrow.commitTrace(SID, TRACE);
        vm.prank(challenger);
        uint256 later = escrow.challenge{value: CBOND}(SID, pid);
        uint256 before = challenger.balance;
        vm.prank(verifier);
        escrow.submitVerdict(later, true);
        assertEq(challenger.balance, before + PAYOUT + CBOND);
    }

    function test_satisfied_pair_does_not_clear_other_sessions_or_promises() public {
        uint256 pid = _register(10 * PAYOUT);
        uint256 otherPid = _register(10 * PAYOUT);
        _openCommit(SID);
        _openCommit(SID2);
        vm.prank(challenger);
        uint256 first = escrow.challenge{value: CBOND}(SID, pid);
        vm.prank(verifier);
        escrow.submitVerdict(first, false);

        vm.startPrank(challenger);
        uint256 otherPromise = escrow.challenge{value: CBOND}(SID, otherPid);
        uint256 otherSession = escrow.challenge{value: CBOND}(SID2, pid);
        vm.stopPrank();
        uint256 before = challenger.balance;
        vm.startPrank(verifier);
        escrow.submitVerdict(otherPromise, true);
        escrow.submitVerdict(otherSession, true);
        vm.stopPrank();
        assertEq(challenger.balance, before + 2 * (PAYOUT + CBOND));
    }

    // ── two-phase commitment ──────────────────────────────────────────────────

    function test_commit_trace_only_session_provider() public {
        vm.prank(provider);
        escrow.openSession(SID, challenger);
        vm.prank(challenger);
        vm.expectRevert(bytes("not session provider"));
        escrow.commitTrace(SID, TRACE);
    }

    function test_commit_trace_only_once() public {
        vm.startPrank(provider);
        escrow.openSession(SID, challenger);
        escrow.commitTrace(SID, TRACE);
        vm.expectRevert(bytes("trace already committed"));
        escrow.commitTrace(SID, keccak256("other"));
        vm.stopPrank();
    }

    function test_challenge_on_unhashed_session_allowed() public {
        uint256 pid = _register(10 * PAYOUT);
        vm.prank(provider);
        escrow.openSession(SID, challenger);              // no commitTrace

        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);
        (, , , , Escrow.Status st, , ) = escrow.challenges(cid);
        assertEq(uint256(st), uint256(Escrow.Status.Open));
    }

    // ── availability: respond / claimDefault / withdrawChallenge ─────────────

    function test_claim_default_after_silence() public {
        uint256 pid = _promiseSession();
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);
        uint256 challengerBefore = challenger.balance;

        vm.warp(block.timestamp + escrow.RESPONSE_WINDOW() + 1);
        escrow.claimDefault(cid);                          // callable by anyone; pays the challenger

        assertEq(challenger.balance, challengerBefore + PAYOUT + CBOND, "default pays like a violation");
        (, , , , uint256 reserve, , ) = escrow.promises(pid);
        assertEq(reserve, 9 * PAYOUT, "reserve drained");
    }

    function test_claim_default_blocked_inside_window_and_after_respond() public {
        uint256 pid = _promiseSession();
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);

        vm.expectRevert(bytes("response window open"));
        escrow.claimDefault(cid);

        vm.prank(provider);
        escrow.respond(cid);
        vm.warp(block.timestamp + escrow.RESPONSE_WINDOW() + 1);
        vm.expectRevert(bytes("provider responded"));
        escrow.claimDefault(cid);
    }

    function test_withdraw_after_verdict_window() public {
        uint256 pid = _promiseSession();
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);
        vm.prank(provider);
        escrow.respond(cid);

        vm.prank(challenger);
        vm.expectRevert(bytes("no exit yet"));
        escrow.withdrawChallenge(cid);

        uint256 challengerBefore = challenger.balance;
        vm.warp(block.timestamp + escrow.VERDICT_WINDOW() + 1);
        vm.prank(challenger);
        escrow.withdrawChallenge(cid);                     // verifier never ruled: bond back, no slash

        assertEq(challenger.balance, challengerBefore + CBOND, "bond refunded");
        (, , , , uint256 reserve, , ) = escrow.promises(pid);
        assertEq(reserve, 10 * PAYOUT, "no slash on verifier outage");
        assertEq(escrow.openChallenges(pid), 0, "counter closed");
    }

    // ── reserve lifecycle ─────────────────────────────────────────────────────

    function test_storm_drains_to_lapse_then_fund_cures() public {
        uint256 pid = _register(2 * PAYOUT);               // covers exactly two claims
        _openCommit(SID);
        _openCommit(SID2);
        _openCommit(SID3);

        vm.startPrank(challenger);
        uint256 c1 = escrow.challenge{value: CBOND}(SID, pid);
        uint256 c2 = escrow.challenge{value: CBOND}(SID2, pid);
        vm.stopPrank();
        vm.prank(verifier);
        escrow.submitVerdict(c1, true);
        vm.prank(verifier);
        escrow.submitVerdict(c2, true);

        // lapsed: third claim rejected at filing, visibly
        vm.prank(challenger);
        vm.expectRevert(bytes("promise lapsed"));
        escrow.challenge{value: CBOND}(SID3, pid);

        // top-up cures the lapse; the third victim can now claim
        vm.prank(provider);
        escrow.fundPromise{value: PAYOUT}(pid);
        vm.prank(challenger);
        uint256 c3 = escrow.challenge{value: CBOND}(SID3, pid);
        vm.prank(verifier);
        escrow.submitVerdict(c3, true);
        (, , , , uint256 reserve, , ) = escrow.promises(pid);
        assertEq(reserve, 0, "all three victims paid");
    }

    function test_lapse_race_loser_exits_with_bond() public {
        uint256 pid = _register(PAYOUT);                   // reserve covers ONE claim
        _openCommit(SID);
        _openCommit(SID2);

        vm.startPrank(challenger);
        uint256 c1 = escrow.challenge{value: CBOND}(SID, pid);   // both pass the filing check
        uint256 c2 = escrow.challenge{value: CBOND}(SID2, pid);
        vm.stopPrank();

        vm.prank(verifier);
        escrow.submitVerdict(c1, true);                    // drains the reserve

        vm.prank(verifier);
        vm.expectRevert(bytes("reserve insufficient"));
        escrow.submitVerdict(c2, true);                    // race loser cannot slash

        uint256 before = challenger.balance;
        vm.prank(challenger);
        escrow.withdrawChallenge(c2);                      // lapsed -> immediate exit with bond
        assertEq(challenger.balance, before + CBOND);
    }

    function test_retroactive_coverage_rejected() public {
        vm.prank(provider);
        escrow.openSession(SID, challenger);               // session first
        vm.warp(block.timestamp + 1);
        uint256 pid = _register(10 * PAYOUT);              // promise registered after open

        vm.prank(challenger);
        vm.expectRevert(bytes("promise postdates session"));
        escrow.challenge{value: CBOND}(SID, pid);
    }

    function test_challenge_window_closes_after_commit() public {
        uint256 pid = _promiseSession();
        vm.warp(block.timestamp + escrow.CHALLENGE_WINDOW() + 1);
        vm.prank(challenger);
        vm.expectRevert(bytes("challenge window closed"));
        escrow.challenge{value: CBOND}(SID, pid);
    }

    function test_uncommitted_session_has_no_expiry() public {
        uint256 pid = _register(10 * PAYOUT);
        vm.prank(provider);
        escrow.openSession(SID, challenger);               // never committed
        vm.warp(block.timestamp + 400 days);
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);
        (, , , , Escrow.Status st, , ) = escrow.challenges(cid);
        assertEq(uint256(st), uint256(Escrow.Status.Open), "perpetual exposure until commit");
    }

    // ── retirement ────────────────────────────────────────────────────────────

    function test_retirement_closes_new_coverage_keeps_old() public {
        uint256 pid = _promiseSession();                   // SID opened before retirement
        vm.prank(provider);
        escrow.retirePromise(pid);

        vm.warp(block.timestamp + 1);
        vm.prank(provider);
        escrow.openSession(SID2, challenger);              // opened after retirement
        vm.prank(challenger);
        vm.expectRevert(bytes("session not covered (retired)"));
        escrow.challenge{value: CBOND}(SID2, pid);

        vm.prank(challenger);                              // old session still covered
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);
        vm.prank(verifier);
        escrow.submitVerdict(cid, true);
    }

    function test_retirement_claims_close_after_window() public {
        uint256 pid = _promiseSession();
        vm.prank(provider);
        escrow.retirePromise(pid);
        vm.warp(block.timestamp + escrow.CHALLENGE_WINDOW() + 1);

        vm.prank(challenger);
        vm.expectRevert(bytes("retirement claims closed"));
        escrow.challenge{value: CBOND}(SID, pid);
    }

    function test_withdraw_reserve_gates() public {
        uint256 pid = _promiseSession();

        vm.prank(provider);
        vm.expectRevert(bytes("not retired"));
        escrow.withdrawReserve(pid);

        vm.prank(provider);
        escrow.retirePromise(pid);
        vm.prank(provider);
        vm.expectRevert(bytes("cooldown"));
        escrow.withdrawReserve(pid);

        vm.warp(block.timestamp + escrow.CHALLENGE_WINDOW() + 1);
        uint256 before = provider.balance;
        vm.prank(provider);
        escrow.withdrawReserve(pid);
        assertEq(provider.balance, before + 10 * PAYOUT, "full reserve returned after cooldown");
    }

    function test_exit_scam_blocked() public {
        // provider sees a bad session, retires immediately, tries to outrun the claim
        uint256 pid = _promiseSession();
        vm.prank(provider);
        escrow.retirePromise(pid);

        // victim files inside the retirement window
        vm.warp(block.timestamp + 5 days);
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);

        // even past the cooldown, the open challenge blocks withdrawal
        vm.warp(block.timestamp + escrow.CHALLENGE_WINDOW() + 1);
        vm.prank(provider);
        vm.expectRevert(bytes("open challenges"));
        escrow.withdrawReserve(pid);

        uint256 before = challenger.balance;
        vm.prank(verifier);
        escrow.submitVerdict(cid, true);                   // paid from still-locked reserve
        assertEq(challenger.balance, before + PAYOUT + CBOND);

        vm.prank(provider);
        escrow.withdrawReserve(pid);                       // remainder only after settlement
    }

    function test_fund_retired_promise_rejected() public {
        uint256 pid = _register(PAYOUT);
        vm.prank(provider);
        escrow.retirePromise(pid);
        vm.prank(provider);
        vm.expectRevert(bytes("promise retired"));
        escrow.fundPromise{value: PAYOUT}(pid);
    }

    // ── audit round: adversarial cases ────────────────────────────────────────

    function test_duplicate_pair_challenge_rejected() public {
        // a second live challenge on one pair can never pay but would pin openChallenges and
        // freeze the reserve withdrawal at dust cost — rejected at filing
        uint256 pid = _promiseSession();
        vm.startPrank(challenger);
        escrow.challenge{value: CBOND}(SID, pid);
        vm.expectRevert(bytes("pair already challenged"));
        escrow.challenge{value: 1}(SID, pid);
        vm.stopPrank();
    }

    function test_pair_rechallengeable_after_withdraw() public {
        uint256 pid = _register(PAYOUT);
        _openCommit(SID);
        _openCommit(SID2);
        vm.startPrank(challenger);
        uint256 c1 = escrow.challenge{value: CBOND}(SID, pid);
        uint256 c2 = escrow.challenge{value: CBOND}(SID2, pid);
        vm.stopPrank();
        vm.prank(verifier);
        escrow.submitVerdict(c1, true);                    // drains the reserve; c2 lapse-raced
        vm.prank(challenger);
        escrow.withdrawChallenge(c2);                      // exit clears the pair lock
        vm.prank(provider);
        escrow.fundPromise{value: PAYOUT}(pid);            // cure
        vm.prank(challenger);
        escrow.challenge{value: CBOND}(SID2, pid);         // same pair, new challenge: accepted
    }

    function test_pair_rechallengeable_after_verifier_outage_withdrawal() public {
        uint256 pid = _promiseSession();
        vm.prank(challenger);
        uint256 first = escrow.challenge{value: CBOND}(SID, pid);
        vm.prank(provider);
        escrow.respond(first);
        vm.warp(block.timestamp + escrow.VERDICT_WINDOW() + 1);
        vm.prank(challenger);
        escrow.withdrawChallenge(first);

        // Withdrawal is not an adjudication. A recovered verifier may still establish a violation.
        vm.prank(challenger);
        uint256 later = escrow.challenge{value: CBOND}(SID, pid);
        uint256 before = challenger.balance;
        vm.prank(verifier);
        escrow.submitVerdict(later, true);
        assertEq(challenger.balance, before + PAYOUT + CBOND);
    }

    function test_claim_default_lapse_race() public {
        // default path hits a drained reserve: claim reverts, the challenger exits with the bond
        uint256 pid = _register(PAYOUT);
        _openCommit(SID);
        _openCommit(SID2);
        vm.startPrank(challenger);
        uint256 c1 = escrow.challenge{value: CBOND}(SID, pid);
        uint256 c2 = escrow.challenge{value: CBOND}(SID2, pid);
        vm.stopPrank();
        vm.prank(verifier);
        escrow.submitVerdict(c1, true);

        vm.warp(block.timestamp + escrow.RESPONSE_WINDOW() + 1);
        vm.expectRevert(bytes("reserve insufficient"));
        escrow.claimDefault(c2);

        uint256 before = challenger.balance;
        vm.prank(challenger);
        escrow.withdrawChallenge(c2);
        assertEq(challenger.balance, before + CBOND);
    }

    function test_slash_through_responded_path() public {
        uint256 pid = _promiseSession();
        vm.prank(challenger);
        uint256 cid = escrow.challenge{value: CBOND}(SID, pid);
        vm.prank(provider);
        escrow.respond(cid);
        uint256 before = challenger.balance;
        vm.prank(verifier);
        escrow.submitVerdict(cid, true);
        assertEq(challenger.balance, before + PAYOUT + CBOND, "responded path still slashes");
    }

    function test_fund_from_stranger() public {
        uint256 pid = _register(PAYOUT);
        address stranger = makeAddr("stranger2");
        vm.deal(stranger, 1 ether);
        vm.prank(stranger);
        escrow.fundPromise{value: PAYOUT}(pid);            // a top-up is a gift
        (, , , , uint256 reserve, , ) = escrow.promises(pid);
        assertEq(reserve, 2 * PAYOUT);
    }

    function test_window_fence_posts() public {
        uint256 pid = _promiseSession();
        (, , , , uint64 committedAt, ) = escrow.sessions(SID);
        vm.warp(committedAt + escrow.CHALLENGE_WINDOW());  // exactly at the boundary: accepted
        vm.prank(challenger);
        escrow.challenge{value: CBOND}(SID, pid);

        uint256 pid2 = _register(PAYOUT);
        vm.prank(provider);
        escrow.retirePromise(pid2);
        (, , , , , , uint64 retiredAt) = escrow.promises(pid2);
        vm.warp(retiredAt + escrow.CHALLENGE_WINDOW());    // exactly at the boundary: still locked
        vm.prank(provider);
        vm.expectRevert(bytes("cooldown"));
        escrow.withdrawReserve(pid2);
    }

    function test_retirement_caps_uncommitted_exposure() public {
        uint256 pid = _register(PAYOUT);
        vm.prank(provider);
        escrow.openSession(SID, challenger);               // never committed: exposure is indefinite...
        vm.prank(provider);
        escrow.retirePromise(pid);                         // ...until retirement bounds it
        vm.warp(block.timestamp + escrow.CHALLENGE_WINDOW() + 1);
        vm.prank(challenger);
        vm.expectRevert(bytes("retirement claims closed"));
        escrow.challenge{value: CBOND}(SID, pid);
    }

    function test_withdraw_reserve_only_once() public {
        uint256 pid = _register(PAYOUT);
        vm.prank(provider);
        escrow.retirePromise(pid);
        vm.warp(block.timestamp + escrow.CHALLENGE_WINDOW() + 1);
        vm.startPrank(provider);
        escrow.withdrawReserve(pid);
        vm.expectRevert(bytes("empty reserve"));
        escrow.withdrawReserve(pid);
        vm.stopPrank();
    }
}
