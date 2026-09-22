// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title  Escrow — promise reserves, session commitments, challenge & settlement (stage 1).
/// @notice The MONEY layer only. Traces live off-chain; their keccak hash is committed here so the
///         off-chain store can hold data it cannot forge (the binding invariant, I1). The `verifier`
///         is a trusted role in stage 1 — the centralized shortcut to remove later via optimistic
///         dispute / TEE.
///
///         Sessions commit in two phases: `openSession` at session start (coverage becomes a
///         visible on-chain fact before any covered action runs) and `commitTrace` at session end.
///         Availability is attributed, not assumed: a challenged provider must either let the
///         verifier rule or `respond` on-chain that the trace is available; silence past
///         RESPONSE_WINDOW lets anyone `claimDefault` — the challenge settles as a violation.
///
///         Promise lifecycle: registered born-funded with a per-promise reserve; each valid slash
///         pays one victim and drains the reserve; reserve < payout = lapse, visible on chain, cured
///         by `fundPromise`; `retirePromise` closes new coverage and starts the withdrawal cooldown.
///         Claims are first-come. Coverage is fixed at session open: a promise covers a session iff
///         it was registered at or before the session opened and not yet retired.
///
///         Clocks. CHALLENGE_WINDOW runs from `commitTrace` — committing is what starts the
///         provider's clock toward expiry, so an uncommitted session stays challengeable
///         indefinitely (the provider ends its own exposure by committing). A retired promise
///         accepts claims only until retiredAt + CHALLENGE_WINDOW, which makes the reserve
///         withdrawal gate a plain timestamp.
contract Escrow {
    enum Status { None, Open, Valid, Invalid, Withdrawn }

    struct Promise {
        address provider;
        bytes32 predicateHash;   // binds the predicate logic (aa_commons), so meaning can't be equivocated
        bytes32 paramsHash;      // binds the promise parameters
        uint256 payout;          // compensation + bounty paid per valid challenge
        uint256 reserve;         // funds backing future payouts; reserve < payout = lapsed (visible)
        uint64  registeredAt;    // coverage is fixed at session open: only earlier promises cover it
        uint64  retiredAt;       // 0 = live; set once by retirePromise
    }

    struct SessionCommit {
        address provider;
        address party;           // the ONLY address allowed to challenge (privacy: your own session)
        bytes32 traceHash;       // commits the off-chain trace -> tamper evidence; zero until committed
        uint64  openedAt;        // openSession timestamp — the user-visible coverage receipt
        uint64  committedAt;     // commitTrace timestamp — starts the challenge window
        bool exists;
    }

    struct Challenge {
        bytes32 sessionId;
        uint256 promiseId;
        address challenger;
        uint256 bond;            // forfeited if the challenge is invalid (anti-spam)
        Status status;
        uint64  challengedAt;    // starts the response clock
        uint64  respondedAt;     // provider's on-chain "trace is available"; zero = silence
    }

    struct TraceCheckpoint {
        uint256 recordCount;
        bytes32 prefixHash;
    }

    // windows (stage 1 constants; per-promise parameters deferred)
    uint64 public constant RESPONSE_WINDOW  = 3 days;   // provider must respond (or the verifier rule) by then
    uint64 public constant VERDICT_WINDOW   = 10 days;  // responded but no verdict -> challenger exits with bond
    uint64 public constant CHALLENGE_WINDOW = 30 days;  // claims accepted this long after commitTrace / retiredAt

    address public owner;
    address public verifier;

    mapping(address => uint256) public bonds;            // provider => forfeited-challenge-bond credit
    mapping(uint256 => Promise) public promises;
    mapping(bytes32 => SessionCommit) public sessions;
    mapping(uint256 => Challenge) public challenges;
    mapping(bytes32 => TraceCheckpoint[]) public traceCheckpoints;
    mapping(bytes32 => bool) public resolved;            // (sessionId, promiseId) paid or final trace cleared
    mapping(bytes32 => bool) public pairChallenged;      // (sessionId, promiseId) has a live challenge
    mapping(uint256 => uint256) public openChallenges;   // promiseId => live challenges (gates withdrawal)
    uint256 public nextPromiseId = 1;
    uint256 public nextChallengeId = 1;

    event BondWithdrawn(address indexed provider, uint256 amount);
    event PromiseRegistered(uint256 indexed promiseId, address indexed provider, bytes32 predicateHash, bytes32 paramsHash, uint256 payout, uint256 reserve);
    event PromiseFunded(uint256 indexed promiseId, uint256 amount, uint256 reserve);
    event PromiseRetired(uint256 indexed promiseId, uint64 retiredAt);
    event ReserveWithdrawn(uint256 indexed promiseId, uint256 amount);
    event SessionOpened(bytes32 indexed sessionId, address indexed provider, address party);
    event TraceCommitted(bytes32 indexed sessionId, bytes32 traceHash);
    event TraceCheckpointed(bytes32 indexed sessionId, uint256 recordCount, bytes32 prefixHash);
    event Challenged(uint256 indexed challengeId, bytes32 indexed sessionId, uint256 indexed promiseId, address challenger, uint256 bond);
    event Responded(uint256 indexed challengeId);
    event Verdict(uint256 indexed challengeId, bool violated, uint256 paidToChallenger);
    event DefaultClaimed(uint256 indexed challengeId, uint256 paidToChallenger);
    event ChallengeWithdrawn(uint256 indexed challengeId);

    modifier onlyOwner() { require(msg.sender == owner, "not owner"); _; }
    modifier onlyVerifier() { require(msg.sender == verifier, "not verifier"); _; }

    constructor(address _verifier) {
        owner = msg.sender;
        verifier = _verifier;
    }

    function setVerifier(address _verifier) external onlyOwner {
        verifier = _verifier;
    }

    // ── promise lifecycle ─────────────────────────────────────────────────────
    /// Register born-funded: the initial reserve rides in msg.value and must cover at least one
    /// payout, so a promise is in force from its first block and solvency is checkable by anyone.
    function registerPromise(bytes32 predicateHash, bytes32 paramsHash, uint256 payout)
        external payable returns (uint256 promiseId)
    {
        require(payout > 0, "zero payout");
        require(msg.value >= payout, "initial reserve below payout");
        promiseId = nextPromiseId++;
        promises[promiseId] = Promise(msg.sender, predicateHash, paramsHash, payout, msg.value,
                                      uint64(block.timestamp), 0);
        emit PromiseRegistered(promiseId, msg.sender, predicateHash, paramsHash, payout, msg.value);
    }

    /// Top up the reserve (anyone; a top-up is a gift). Cures a lapse.
    function fundPromise(uint256 promiseId) external payable {
        Promise storage p = promises[promiseId];
        require(p.provider != address(0), "no promise");
        require(p.retiredAt == 0, "promise retired");
        require(msg.value > 0, "zero amount");
        p.reserve += msg.value;
        emit PromiseFunded(promiseId, msg.value, p.reserve);
    }

    /// Close new coverage and start the withdrawal cooldown. Sessions opened before retiredAt stay
    /// challengeable until retiredAt + CHALLENGE_WINDOW; sessions opened after are not covered.
    function retirePromise(uint256 promiseId) external {
        Promise storage p = promises[promiseId];
        require(msg.sender == p.provider, "not promise provider");
        require(p.retiredAt == 0, "already retired");
        p.retiredAt = uint64(block.timestamp);
        emit PromiseRetired(promiseId, p.retiredAt);
    }

    /// The exit that cannot outrun a claim: gated on the cooldown AND zero live challenges, so a
    /// victim who files within the window is always paid from still-locked money.
    function withdrawReserve(uint256 promiseId) external {
        Promise storage p = promises[promiseId];
        require(msg.sender == p.provider, "not promise provider");
        require(p.retiredAt != 0, "not retired");
        require(block.timestamp > p.retiredAt + CHALLENGE_WINDOW, "cooldown");
        require(openChallenges[promiseId] == 0, "open challenges");
        uint256 amount = p.reserve;
        require(amount > 0, "empty reserve");
        p.reserve = 0;                                      // effects
        (bool ok, ) = msg.sender.call{value: amount}("");   // interaction last
        require(ok, "transfer failed");
        emit ReserveWithdrawn(promiseId, amount);
    }

    /// Forfeited challenge bonds accrue as withdrawable credit.
    function withdrawBond(uint256 amount) external {
        require(bonds[msg.sender] >= amount, "exceeds credit");
        bonds[msg.sender] -= amount;                        // effects
        (bool ok, ) = msg.sender.call{value: amount}("");   // interaction last
        require(ok, "transfer failed");
        emit BondWithdrawn(msg.sender, amount);
    }

    // ── session commitment (two phases) ───────────────────────────────────────
    /// Phase 1, at session START. Coverage means exactly "opened on chain" — the party can check
    /// this receipt (and that `party` is their address) before trusting the session.
    function openSession(bytes32 sessionId, address party) external {
        require(!sessions[sessionId].exists, "session exists");
        sessions[sessionId] = SessionCommit(msg.sender, party, bytes32(0), uint64(block.timestamp), 0, true);
        emit SessionOpened(sessionId, msg.sender, party);
    }

    /// Phase 2, at session END. Committing starts the challenge window; an opened session whose
    /// hash never lands stays challengeable indefinitely and settles by default when silent.
    function commitTrace(bytes32 sessionId, bytes32 traceHash) external {
        SessionCommit storage s = sessions[sessionId];
        require(s.exists, "no session");
        require(msg.sender == s.provider, "not session provider");
        require(s.traceHash == bytes32(0), "trace already committed");
        require(traceHash != bytes32(0), "zero trace hash");
        s.traceHash = traceHash;
        s.committedAt = uint64(block.timestamp);
        emit TraceCommitted(sessionId, traceHash);
    }

    /// Anchor a cumulative prefix without finalizing the session or starting its challenge clock.
    /// The trusted verifier checks EVERY historical prefix against the eventual final trace.
    function checkpointTrace(bytes32 sessionId, uint256 recordCount, bytes32 prefixHash) external {
        SessionCommit storage s = sessions[sessionId];
        require(s.exists, "no session");
        require(msg.sender == s.provider, "not session provider");
        require(s.traceHash == bytes32(0), "trace already committed");
        require(recordCount > 0, "empty checkpoint");
        require(prefixHash != bytes32(0), "zero checkpoint hash");
        TraceCheckpoint[] storage checkpoints = traceCheckpoints[sessionId];
        require(checkpoints.length == 0 || recordCount > checkpoints[checkpoints.length - 1].recordCount,
                "checkpoint count not increasing");
        checkpoints.push(TraceCheckpoint(recordCount, prefixHash));
        emit TraceCheckpointed(sessionId, recordCount, prefixHash);
    }

    function checkpointCount(bytes32 sessionId) external view returns (uint256) {
        return traceCheckpoints[sessionId].length;
    }

    // ── challenger side ───────────────────────────────────────────────────────
    function challenge(bytes32 sessionId, uint256 promiseId) external payable returns (uint256 challengeId) {
        SessionCommit memory s = sessions[sessionId];
        require(s.exists, "no session");
        require(msg.sender == s.party, "not session party");   // privacy: only a party may challenge
        Promise memory p = promises[promiseId];
        require(p.provider != address(0), "no promise");
        // BINDING (I1): the challenged promise must belong to the SAME provider who ran the session.
        require(p.provider == s.provider, "promise/session provider mismatch");
        // coverage is fixed at session open: what was registered when you started is the deal
        require(p.registeredAt <= s.openedAt, "promise postdates session");
        if (p.retiredAt != 0) {
            require(s.openedAt <= p.retiredAt, "session not covered (retired)");
            require(block.timestamp <= p.retiredAt + CHALLENGE_WINDOW, "retirement claims closed");
        }
        // the challenge window runs from commit; an uncommitted session has no expiry
        if (s.committedAt != 0) {
            require(block.timestamp <= s.committedAt + CHALLENGE_WINDOW, "challenge window closed");
        }
        require(p.reserve >= p.payout, "promise lapsed");      // claims are first-come; lapse is visible
        bytes32 k = _pairKey(sessionId, promiseId);
        require(!resolved[k], "pair already resolved");        // completed adjudications cannot be reopened
        // one LIVE challenge per pair: a second concurrent filing can never pay (the pair pays at
        // most once) but would pin openChallenges and freeze the reserve withdrawal at dust cost.
        require(!pairChallenged[k], "pair already challenged");
        require(msg.value > 0, "zero challenge bond");
        challengeId = nextChallengeId++;
        challenges[challengeId] = Challenge(sessionId, promiseId, msg.sender, msg.value, Status.Open,
                                            uint64(block.timestamp), 0);
        pairChallenged[k] = true;
        openChallenges[promiseId] += 1;
        emit Challenged(challengeId, sessionId, promiseId, msg.sender, msg.value);
    }

    // ── availability: respond / default / withdraw ────────────────────────────
    /// The provider's on-chain "the trace is available". Turns off the default path; the verifier
    /// then rules. Responding and still withholding is adjudicated as a violation (trusted verifier).
    function respond(uint256 challengeId) external {
        Challenge storage c = challenges[challengeId];
        require(c.status == Status.Open, "not open");
        require(msg.sender == sessions[c.sessionId].provider, "not session provider");
        require(c.respondedAt == 0, "already responded");
        c.respondedAt = uint64(block.timestamp);
        emit Responded(challengeId);
    }

    /// The store rule: no trace produced (no verdict, no response) within the window -> the
    /// challenge settles as a violation. Callable by anyone; pays the challenger.
    function claimDefault(uint256 challengeId) external {
        Challenge storage c = challenges[challengeId];
        require(c.status == Status.Open, "not open");
        require(c.respondedAt == 0, "provider responded");
        require(block.timestamp > c.challengedAt + RESPONSE_WINDOW, "response window open");
        _slash(c, challengeId, true);
    }

    /// Challenger exit when no verdict can come: the reserve was drained by earlier claims (lapse
    /// race), or the provider responded but the verifier never ruled within the verdict window.
    /// Bond back, nobody slashed, the pair stays challengeable.
    function withdrawChallenge(uint256 challengeId) external {
        Challenge storage c = challenges[challengeId];
        require(c.status == Status.Open, "not open");
        require(msg.sender == c.challenger, "not challenger");
        Promise memory p = promises[c.promiseId];
        bool lapsed = p.reserve < p.payout;
        bool verdictOverdue = c.respondedAt != 0 && block.timestamp > c.challengedAt + VERDICT_WINDOW;
        require(lapsed || verdictOverdue, "no exit yet");
        c.status = Status.Withdrawn;                           // effects
        pairChallenged[_pairKey(c.sessionId, c.promiseId)] = false;
        openChallenges[c.promiseId] -= 1;
        (bool ok, ) = c.challenger.call{value: c.bond}("");    // interaction last
        require(ok, "refund failed");
        emit ChallengeWithdrawn(challengeId);
    }

    // ── verifier (trusted role, stage 1) ──────────────────────────────────────
    function submitVerdict(uint256 challengeId, bool violated) external onlyVerifier {
        Challenge storage c = challenges[challengeId];
        require(c.status == Status.Open, "not open");          // double-verdict guard
        if (violated) {
            _slash(c, challengeId, false);
        } else {
            // invalid: forfeit the challenge bond to the provider as credit (pull over push)
            c.status = Status.Invalid;
            bytes32 k = _pairKey(c.sessionId, c.promiseId);
            // A cleared final trace cannot later earn a default payout during an outage.
            // Without a final commitment, later actions must remain challengeable.
            if (sessions[c.sessionId].traceHash != bytes32(0)) resolved[k] = true;
            pairChallenged[k] = false;
            openChallenges[c.promiseId] -= 1;
            bonds[sessions[c.sessionId].provider] += c.bond;
            emit Verdict(challengeId, false, 0);
        }
    }

    /// Shared settlement for a violation (verifier verdict or default claim): drain the reserve by
    /// one payout, pay the challenger payout + bond. The promise stays registered; reserve < payout
    /// is the (curable, visible) lapse state.
    function _slash(Challenge storage c, uint256 challengeId, bool byDefault) internal {
        Promise storage p = promises[c.promiseId];
        require(p.provider == sessions[c.sessionId].provider, "provider mismatch");  // defense-in-depth (I1)
        bytes32 k = _pairKey(c.sessionId, c.promiseId);
        require(!resolved[k], "pair already resolved");        // guard concurrent opens on the same pair
        require(p.reserve >= p.payout, "reserve insufficient");  // lapse race: loser exits via withdrawChallenge
        // effects (state first)
        resolved[k] = true;
        pairChallenged[k] = false;
        c.status = Status.Valid;
        openChallenges[c.promiseId] -= 1;
        p.reserve -= p.payout;
        uint256 amount = p.payout + c.bond;                    // compensation + bounty + bond refund
        // interaction (transfer last)
        (bool ok, ) = c.challenger.call{value: amount}("");
        require(ok, "payout failed");
        if (byDefault) {
            emit DefaultClaimed(challengeId, amount);
        } else {
            emit Verdict(challengeId, true, amount);
        }
    }

    function _pairKey(bytes32 sessionId, uint256 promiseId) internal pure returns (bytes32) {
        return keccak256(abi.encode(sessionId, promiseId));
    }
}
